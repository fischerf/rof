"""
factcheck/run_factcheck.py
==========================
ROF Showcase: News Credibility Assessment with Learned Routing Confidence
=========================================================================

Domain
------
Automatic fact-checking of news articles. Each article flows through a
six-stage pipeline that extracts claims, verifies sources, cross-references
facts, detects bias, produces a credibility verdict, and formats a report.

What this showcase demonstrates
---------------------------------
  CLI   — Each .rl stage file is independently lintable and inspectable.
           The pipeline.yaml lets you run the standard pipeline via CLI.
  Tools — Six deterministic tools cover every goal; the LLM handles only
           narrative tasks that require contextual reasoning.
  Routing — ConfidentPipeline replaces the standard Pipeline transparently.
           On the first article: Tier 1 (static) only.
           From article 2 onward: Tier 2/3 confidence shapes dispatch.
           By article 5: historical memory dominates for all known patterns.
  Traces — Every routing decision is a RoutingTrace entity in the snapshot.
  Memory — RoutingMemory persists across articles (simulating production use).

Run
---
  # Full showcase (no API key needed — uses scripted LLM stub):
  python tests/fixtures/pipeline_fakenews_detection/run_factcheck.py

  # Individual stage testing via CLI (real LLM, needs ROF_API_KEY):
  rof lint    tests/fixtures/pipeline_fakenews_detection/01_extract.rl
  rof lint    tests/fixtures/pipeline_fakenews_detection/02_verify_source.rl --strict
  rof inspect tests/fixtures/pipeline_fakenews_detection/01_extract.rl
  rof inspect tests/fixtures/pipeline_fakenews_detection/05_decide.rl --format json
  rof run     tests/fixtures/pipeline_fakenews_detection/01_extract.rl --provider anthropic
  rof pipeline run tests/fixtures/pipeline_fakenews_detection/pipeline.yaml --provider anthropic

Requirements
------------
  pip install -e ".[pipeline]"  (installs rof + pyyaml)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

# ── ANSI helpers ─────────────────────────────────────────────────────────────
import shutil as _sh
_COLOUR = _sh.get_terminal_size().columns > 0 and sys.stdout.isatty()

def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if _COLOUR else t

H1   = lambda t: _c("1;36",  t)
H2   = lambda t: _c("1;33",  t)
OK   = lambda t: _c("32",    t)
WARN = lambda t: _c("33",    t)
ERR  = lambda t: _c("31",    t)
DIM  = lambda t: _c("2",     t)
CODE = lambda t: _c("35",    t)
TIER = lambda t: _c("1;34",  t)
VAL  = lambda t: _c("36",    t)
BOLD = lambda t: _c("1",     t)

logging.basicConfig(level=logging.WARNING)

SEP  = "═" * 72
SEP2 = "─" * 72

# Current file's directory — used to build paths to .rl files.
HERE = Path(__file__).parent

def section(title: str) -> None:
    print(f"\n{SEP}")
    print(H1(f"  {title}"))
    print(f"{SEP}\n")

def subsection(t: str) -> None:
    print(H2(f"\n  ▶ {t}"))

def note(msg: str) -> None:
    print(f"  {DIM('·')} {msg}")

def cmd_line(cmd: str) -> None:
    print(f"  {CODE('$')} {CODE(cmd)}")

def blank() -> None:
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# rof imports
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from rof_framework.rof_core import (
        LLMProvider, LLMRequest, LLMResponse,
        ToolProvider, ToolRequest, ToolResponse,
        RLParser, InMemoryStateAdapter, EventBus,
    )
except ImportError:
    sys.exit("✗  rof_framework not found. Add src/ to PYTHONPATH or install the package.")

try:
    from rof_framework.rof_pipeline import PipelineStage, FanOutGroup, OnFailure, SnapshotMerge
except ImportError:
    sys.exit("✗  rof_framework.rof_pipeline not found.")

try:
    from rof_framework.rof_routing import (
        ConfidentPipeline, RoutingMemory,
        RoutingMemoryInspector, GoalPatternNormalizer,
    )
except ImportError:
    sys.exit("✗  rof_framework.rof_routing not found.")


# ═══════════════════════════════════════════════════════════════════════════════
# Sample articles — three news items of varying credibility
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SampleArticle:
    id:           str
    headline:     str
    body:         str
    domain:       str
    author:       str
    published_at: str
    # Ground truth (for validation at the end)
    expected_label: str

ARTICLES = [
    SampleArticle(
        id           = "A001",
        headline     = "Central Bank Raises Interest Rates by 0.25% to Combat Inflation",
        body         = (
            "The central bank announced a quarter-point interest rate increase on "
            "Tuesday, bringing the benchmark rate to 5.5%. Bank governor Sarah Chen "
            "cited persistent core inflation of 3.2% as the primary driver. The "
            "decision was unanimous among the seven-member board. Markets had widely "
            "anticipated the move, with futures pricing in an 89% probability of the "
            "hike. Mortgage rates are expected to rise within weeks."
        ),
        domain       = "reuters.com",
        author       = "Michael Torres",
        published_at = "2025-10-28",
        expected_label = "likely_true",
    ),
    SampleArticle(
        id           = "A002",
        headline     = "New Study: Coffee Cures All Forms of Cancer, Scientists Confirm",
        body         = (
            "Researchers at a leading university have PROVEN that drinking three cups "
            "of coffee per day eliminates cancer cells 100% of the time. The "
            "bombshell study, suppressed by Big Pharma for years, shows that caffeine "
            "destroys every known cancer type within 30 days. Doctors are FURIOUS "
            "they don't want you to know this! Share before it gets deleted! "
            "The FDA has been hiding this miracle cure since 2018."
        ),
        domain       = "healthmiracle247.net",
        author       = "Anonymous",
        published_at = "2025-11-02",
        expected_label = "likely_false",
    ),
    SampleArticle(
        id           = "A003",
        headline     = "City Council Votes 6-3 to Approve New Public Transit Expansion",
        body         = (
            "After three hours of public comment, the city council voted 6 to 3 to "
            "approve the $2.4 billion transit expansion plan that will add 18 miles "
            "of light rail and 12 new bus rapid transit corridors. Construction is "
            "expected to begin in Q2 2026. Critics argue the cost is too high; "
            "supporters say it will reduce car traffic by an estimated 15%. The "
            "mayor is expected to sign the ordinance this week."
        ),
        domain       = "citynewsjournal.com",
        author       = "Priya Nair",
        published_at = "2025-11-05",
        expected_label = "likely_true",
    ),
    SampleArticle(
        id           = "A004",
        headline     = "LEAKED: Government Planning Secret 5G Mind Control Program",
        body         = (
            "Whistleblowers have exposed a classified government program to use 5G "
            "towers to control the population. Documents obtained by our journalists "
            "reveal that frequencies are being modulated to induce compliance. "
            "Top scientists who spoke out have been silenced. The mainstream media "
            "refuses to cover this story because they are part of the agenda. "
            "Protect yourself now before it is too late. Share this truth."
        ),
        domain       = "truthwatchersnow.org",
        author       = "The Editor",
        published_at = "2025-10-30",
        expected_label = "likely_false",
    ),
    SampleArticle(
        id           = "A005",
        headline     = "Quarterly Earnings: Tech Sector Reports Mixed Results Amid AI Spending",
        body         = (
            "Technology companies reported mixed quarterly results this week, with "
            "earnings beating expectations at four of the five largest firms but "
            "revenue guidance coming in below analyst consensus. Heavy AI infrastructure "
            "spending weighed on margins across the sector. The composite tech index "
            "fell 1.8% on Friday after guidance was released. Analysts remain divided "
            "on whether AI capital expenditures will produce returns by 2027."
        ),
        domain       = "wsj.com",
        author       = "Emma Whitfield",
        published_at = "2025-11-07",
        expected_label = "likely_true",
    ),
]

# ═══════════════════════════════════════════════════════════════════════════════
# Tool suite — six deterministic fact-check tools
# ═══════════════════════════════════════════════════════════════════════════════

# Source credibility database (simplified)
_SOURCE_DB: dict[str, dict] = {
    "reuters.com":            {"credibility_score": 0.92, "bias": "center", "known_satire": 0, "type": "wire_service"},
    "wsj.com":                {"credibility_score": 0.88, "bias": "center_right", "known_satire": 0, "type": "newspaper"},
    "citynewsjournal.com":    {"credibility_score": 0.71, "bias": "center", "known_satire": 0, "type": "local_news"},
    "healthmiracle247.net":   {"credibility_score": 0.08, "bias": "sensationalist", "known_satire": 0, "type": "pseudoscience"},
    "truthwatchersnow.org":   {"credibility_score": 0.05, "bias": "far_right", "known_satire": 0, "type": "conspiracy"},
    "rapidnewsdaily.com":     {"credibility_score": 0.35, "bias": "tabloid", "known_satire": 0, "type": "tabloid"},
}
_DEFAULT_SOURCE = {"credibility_score": 0.45, "bias": "unknown", "known_satire": 0, "type": "unknown"}

# Claim fact database (simplified for demo)
_FACT_DB: dict[str, bool] = {
    "central bank raised interest rates": True,
    "benchmark rate at 5.5 percent": True,
    "core inflation at 3.2 percent": True,
    "coffee cures cancer": False,
    "caffeine destroys cancer": False,
    "fda suppressed coffee cure": False,
    "city council voted 6 to 3": True,
    "transit expansion cost 2.4 billion": True,
    "government 5g mind control program": False,
    "5g frequencies used for compliance": False,
    "tech sector mixed earnings": True,
    "ai spending weighs on margins": True,
}

# Emotional / clickbait signal words
_EMOTIONAL_WORDS = [
    "bombshell", "secret", "leaked", "furious", "suppressed", "miracle",
    "shocking", "exposed", "truth", "they don't want you to know",
    "share before it gets deleted", "silenced", "agenda", "proof",
    "confirmed", "100%", "all forms", "every known", "too late",
]


class ClaimExtractorTool(ToolProvider):
    """Extracts discrete factual claims from article body text."""

    @property
    def name(self) -> str: return "ClaimExtractorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["extract", "claims", "claim", "facts", "statements"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities    = req.input
        article     = entities.get("Article", {})
        body        = str(article.get("body", "")).lower()
        headline    = str(article.get("headline", "")).lower()
        full_text   = headline + " " + body

        # Count matched fact-DB entries as proxy for claim extraction
        matched     = [k for k in _FACT_DB if k in full_text]
        total_words = len(full_text.split())

        return ToolResponse(success=True, output={
            "ClaimSet": {
                "total_claims":      len(matched),
                "word_count":        total_words,
                "matched_claims":    ", ".join(matched[:5]) if matched else "none",
                "has_statistics":    any(c.isdigit() for c in full_text),
                "has_attribution":   any(w in full_text for w in ["said", "announced", "confirmed", "according"]),
                "extraction_method": "keyword_scan",
            }
        })


class SourceLookupTool(ToolProvider):
    """Looks up domain metadata from the article's URL/domain."""

    @property
    def name(self) -> str: return "SourceLookupTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["identify", "source", "information", "domain", "publication"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities = req.input
        article  = entities.get("Article", {})
        domain   = str(article.get("domain", "unknown.com"))
        author   = str(article.get("author", "Unknown"))

        db_entry = _SOURCE_DB.get(domain, _DEFAULT_SOURCE)
        return ToolResponse(success=True, output={
            "SourceInfo": {
                "domain":       domain,
                "author":       author,
                "source_type":  db_entry["type"],
                "indexed":      domain in _SOURCE_DB,
                "country":      "US",
            }
        })


class SourceCredibilityTool(ToolProvider):
    """Retrieves publisher credibility and author reputation scores."""

    @property
    def name(self) -> str: return "SourceCredibilityTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["credibility", "lookup", "source", "verify", "author", "credentials"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities    = req.input
        source_info = entities.get("SourceInfo", {})
        domain      = str(source_info.get("domain", "unknown.com"))
        author      = str(source_info.get("author", "Unknown"))

        db_entry    = _SOURCE_DB.get(domain, _DEFAULT_SOURCE)
        author_score = 0.50 if author in ("Anonymous", "Unknown", "The Editor", "Staff Reporter") else 0.75

        return ToolResponse(success=True, output={
            "SourceProfile": {
                "credibility_score": db_entry["credibility_score"],
                "political_bias":    db_entry["bias"],
                "known_satire":      db_entry["known_satire"],
                "source_type":       db_entry["type"],
                "author_score":      author_score,
                "indexed_in_db":     domain in _SOURCE_DB,
            }
        })


class CrossReferenceTool(ToolProvider):
    """Cross-references claims against a curated fact database."""

    @property
    def name(self) -> str: return "CrossReferenceTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["cross_reference", "cross", "reference", "check", "statistical", "verify", "accuracy"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities = req.input
        article  = entities.get("Article", {})
        claim_set= entities.get("ClaimSet", {})
        body     = str(article.get("body", "")).lower()
        headline = str(article.get("headline", "")).lower()
        full_text= headline + " " + body

        confirmed  = [k for k, v in _FACT_DB.items() if v and k in full_text]
        disputed   = [k for k, v in _FACT_DB.items() if not v and k in full_text]
        total      = len(confirmed) + len(disputed)

        return ToolResponse(success=True, output={
            "VerificationResult": {
                "confirmed_count": len(confirmed),
                "disputed_count":  len(disputed),
                "unverified_count":max(0, int(claim_set.get("total_claims", 2)) - total),
                "confirmed_claims":  ", ".join(confirmed[:3]) if confirmed else "none",
                "disputed_claims":   ", ".join(disputed[:3]) if disputed else "none",
                "verification_rate": round(total / max(total + 1, 1), 3),
            }
        })


class BiasDetectorTool(ToolProvider):
    """Detects emotional language, bias patterns, and clickbait signals."""

    @property
    def name(self) -> str: return "BiasDetectorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["bias", "analyze", "emotional", "detect", "sentiment", "clickbait", "patterns"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities = req.input
        article  = entities.get("Article", {})
        body     = str(article.get("body", "")).lower()
        headline = str(article.get("headline", "")).lower()
        full_text= headline + " " + body

        emotional_hits  = [w for w in _EMOTIONAL_WORDS if w in full_text]
        emotional_score = min(len(emotional_hits) / 5.0, 1.0)
        clickbait_count = sum(1 for w in ["leaked", "secret", "they don't want", "share before", "too late"] if w in full_text)
        all_caps_words  = sum(1 for w in full_text.upper().split() if w == w and len(w) > 3 and w.isalpha() and w in body.upper())

        # Very rough political lean from source_profile
        src_profile = entities.get("SourceProfile", {})
        bias        = str(src_profile.get("political_bias", "unknown"))

        return ToolResponse(success=True, output={
            "BiasProfile": {
                "emotional_score":   round(emotional_score, 3),
                "emotional_signals": ", ".join(emotional_hits[:4]) if emotional_hits else "none",
                "clickbait_signals": clickbait_count,
                "political_lean":    bias,
                "all_caps_count":    min(all_caps_words, 5),
            }
        })


class CredibilityScorerTool(ToolProvider):
    """Aggregates source, verification, and bias signals into a final score."""

    @property
    def name(self) -> str: return "CredibilityScorerTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["score", "credibility", "aggregate", "signals", "overall", "calculate"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities     = req.input
        src          = entities.get("SourceProfile", {})
        verify       = entities.get("VerificationResult", {})
        bias         = entities.get("BiasProfile", {})

        src_score    = float(src.get("credibility_score",  0.5))
        confirmed    = int(verify.get("confirmed_count",   0))
        disputed     = int(verify.get("disputed_count",    0))
        emotional    = float(bias.get("emotional_score",   0.5))
        clickbait    = int(bias.get("clickbait_signals",   0))

        verify_score = (confirmed - disputed * 1.5) / max(confirmed + disputed + 1, 1)
        verify_score = max(0.0, min(1.0, verify_score * 0.5 + 0.5))

        penalty      = min(emotional * 0.25 + clickbait * 0.08, 0.40)

        composite    = (src_score * 0.45 + verify_score * 0.40) - penalty

        if composite > 0.70:
            label = "likely_true"
        elif composite > 0.45:
            label = "uncertain"
        elif composite > 0.20:
            label = "likely_misleading"
        else:
            label = "likely_false"

        # Override for known conspiracy / pseudoscience domains
        if src_score < 0.15:
            label = "likely_false"

        return ToolResponse(success=True, output={
            "CredibilityVerdict": {
                "credibility_score":  round(max(0.0, min(1.0, composite)), 4),
                "label":              label,
                "source_weight":      0.45,
                "verification_weight":0.40,
                "bias_penalty":       round(penalty, 3),
            }
        })


class ReportFormatterTool(ToolProvider):
    """Structures the final fact-check report from accumulated entity state."""

    @property
    def name(self) -> str: return "ReportFormatterTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["generate", "report", "compile", "evidence", "format", "summary"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities = req.input
        article  = entities.get("Article", {})
        verdict  = entities.get("CredibilityVerdict", {})
        src      = entities.get("SourceProfile", {})
        verify   = entities.get("VerificationResult", {})
        bias     = entities.get("BiasProfile", {})

        score    = float(verdict.get("credibility_score", 0.5))
        label    = str(verdict.get("label", "uncertain"))
        rating   = {
            "likely_true":       "✅ LIKELY TRUE",
            "uncertain":         "⚠️  UNCERTAIN",
            "likely_misleading": "🟠 LIKELY MISLEADING",
            "likely_false":      "❌ LIKELY FALSE",
        }.get(label, "❓ UNRATED")

        domain   = str(article.get("domain",   "unknown"))
        headline = str(article.get("headline", "unknown"))
        src_type = str(src.get("source_type", "unknown"))
        src_cred = float(src.get("credibility_score", 0.5))
        disputed = int(verify.get("disputed_count", 0))
        emotional= float(bias.get("emotional_score", 0.0))

        # Build evidence bullet list
        bullets: list[str] = []
        if src_cred >= 0.75:
            bullets.append(f"Source '{domain}' is a highly credible {src_type}")
        elif src_cred < 0.25:
            bullets.append(f"Source '{domain}' ({src_type}) has very low credibility")
        if disputed > 0:
            bullets.append(f"{disputed} claim(s) disputed by fact database")
        if emotional > 0.5:
            bullets.append(f"High emotional language detected (score {emotional:.2f})")
        if int(bias.get("clickbait_signals", 0)) > 1:
            bullets.append("Multiple clickbait signals present")

        evidence_str = " | ".join(bullets) if bullets else "Insufficient signals"

        return ToolResponse(success=True, output={
            "FactCheckReport": {
                "rating":          rating,
                "credibility_pct": round(score * 100, 1),
                "headline":        headline,
                "publisher":       domain,
                "key_evidence":    evidence_str,
                "status":          "complete",
            }
        })


ALL_TOOLS = [
    ClaimExtractorTool(),
    SourceLookupTool(),
    SourceCredibilityTool(),
    CrossReferenceTool(),
    BiasDetectorTool(),
    CredibilityScorerTool(),
    ReportFormatterTool(),
]

STAGE_RL_FILES = [
    "01_extract.rl",
    "02_verify_source.rl",
    "03_cross_reference.rl",
    "04_bias_analysis.rl",
    "05_decide.rl",
    "06_report.rl",
]

STAGE_NAMES = ["extract", "verify_source", "cross_reference", "bias_analysis", "decide", "report"]


# ═══════════════════════════════════════════════════════════════════════════════
# Scripted LLM — deterministic responses for narrative goals
# ═══════════════════════════════════════════════════════════════════════════════

class FactCheckLLM(LLMProvider):
    """
    Returns stage-appropriate RL for goals that no tool covers.
    The LLM path is intentionally narrow: only narrative/interpretive goals.
    """

    _RESPONSES: dict[str, str] = {
        "assess article structure and narrative": (
            'Article has narrative_type of "news_report".\n'
            'Article has attribution_density of "medium".\n'
            'Article has source_diversity of 1.'
        ),
        "assess evidence quality for verificationresult": (
            'VerificationResult has evidence_quality of "structured".\n'
            'VerificationResult has source_specificity of "moderate".'
        ),
        "interpret framing and context of article": (
            'BiasProfile has framing_type of "factual".\n'
            'BiasProfile has appeal_type of "logos".'
        ),
        "determine credibilityverdict final label": (
            'CredibilityVerdict has llm_assessment of "signals reviewed".\n'
            'CredibilityVerdict has reasoning of "Based on source credibility and claim verification".'
        ),
        "write executive summary for factcheckreport": (
            'FactCheckReport has executive_summary of "Assessment complete based on multi-signal analysis".\n'
            'FactCheckReport has recommended_action of "review full report before sharing".'
        ),
    }

    def complete(self, request: LLMRequest) -> LLMResponse:
        goal_expr = ""
        for line in reversed(request.prompt.splitlines()):
            line = line.strip().rstrip(".")
            if line.lower().startswith("ensure "):
                goal_expr = line[7:].strip().lower()
                break
        content = self._RESPONSES.get(goal_expr, 'Result has status of "processed".')
        return LLMResponse(content=content, raw={})

    def supports_tool_calling(self) -> bool: return False

    @property
    def context_limit(self) -> int: return 8192


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline builder — injects Article attributes before running
# ═══════════════════════════════════════════════════════════════════════════════

def _article_preamble(article: SampleArticle) -> str:
    """Prepend article data as RL attributes to Stage 1 source."""
    body_escaped = article.body.replace('"', '\\"')
    return (
        f'// Article: {article.id}\n'
        f'Article has article_id of "{article.id}".\n'
        f'Article has headline of "{article.headline}".\n'
        f'Article has body of "{body_escaped}".\n'
        f'Article has domain of "{article.domain}".\n'
        f'Article has author of "{article.author}".\n'
        f'Article has published_at of "{article.published_at}".\n\n'
    )


def _build_stages(article: SampleArticle) -> list[PipelineStage]:
    """Build the six pipeline stages for one article."""
    stage_sources = {}
    for name, rl_file in zip(STAGE_NAMES, STAGE_RL_FILES):
        path   = HERE / rl_file
        source = path.read_text(encoding="utf-8")
        # Inject article data only into stage 1; all others get it via context
        if name == "extract":
            source = _article_preamble(article) + source
        stage_sources[name] = source

    return [
        PipelineStage(name, rl_source=stage_sources[name], description=name)
        for name in STAGE_NAMES
    ]


def _build_pipeline(
    article:        SampleArticle,
    routing_memory: RoutingMemory,
    bus:            Optional[EventBus] = None,
) -> ConfidentPipeline:
    return ConfidentPipeline(
        steps                = _build_stages(article),
        llm_provider         = FactCheckLLM(),
        tools                = ALL_TOOLS,
        routing_memory       = routing_memory,
        write_routing_traces = True,
        bus                  = bus,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers for reading routing traces from snapshot
# ═══════════════════════════════════════════════════════════════════════════════

def _traces(snapshot: dict) -> list[dict]:
    out = []
    for name, ent in snapshot.get("entities", {}).items():
        if not name.startswith("RoutingTrace"):
            continue
        a = ent.get("attributes", {})
        out.append({
            "entity":    name,
            "pattern":   a.get("goal_pattern", ""),
            "tool":      a.get("tool_selected", "LLM"),
            "static":    float(a.get("static_confidence",  0)),
            "session":   float(a.get("session_confidence", 0)),
            "hist":      float(a.get("hist_confidence",    0)),
            "composite": float(a.get("composite",          0)),
            "tier":      a.get("dominant_tier", "static"),
            "sat":       float(a.get("satisfaction",       0)),
            "uncertain": a.get("is_uncertain", "False") == "True",
            "stage":     a.get("stage", ""),
        })
    out.sort(key=lambda x: (x["stage"], x["pattern"]))
    return out


def _report(snapshot: dict) -> dict:
    """Extract FactCheckReport attributes."""
    ent = snapshot.get("entities", {}).get("FactCheckReport", {})
    return ent.get("attributes", {})


def _verdict(snapshot: dict) -> dict:
    ent = snapshot.get("entities", {}).get("CredibilityVerdict", {})
    return ent.get("attributes", {})


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1 — CLI workflow (lint + inspect, shown and optionally executed)
# ═══════════════════════════════════════════════════════════════════════════════

def section_cli_workflow() -> None:
    section("1 · CLI Workflow — Lint, Inspect, Individual Stage Run")

    note("Every .rl stage file is fully self-contained and CLI-compatible.")
    note("Use these commands to develop, validate, and debug stages independently.")
    note("The pipeline.yaml lets you run the full pipeline via CLI with a real LLM.")
    blank()

    cli_cmds = [
        ("Lint all stages",
         [f"rof lint tests/fixtures/pipeline_fakenews_detection/{f}" for f in STAGE_RL_FILES]),
        ("Lint with strict mode (warnings → errors)",
         ["rof lint tests/fixtures/pipeline_fakenews_detection/05_decide.rl --strict"]),
        ("Inspect stage AST",
         ["rof inspect tests/fixtures/pipeline_fakenews_detection/01_extract.rl",
          "rof inspect tests/fixtures/pipeline_fakenews_detection/05_decide.rl --format json"]),
        ("Run a single stage (needs ROF_PROVIDER + ROF_API_KEY)",
         ["rof run tests/fixtures/pipeline_fakenews_detection/01_extract.rl --provider anthropic",
          "rof run tests/fixtures/pipeline_fakenews_detection/01_extract.rl --provider openai --verbose",
          "rof debug tests/fixtures/pipeline_fakenews_detection/05_decide.rl --step"]),
        ("Run the full pipeline via YAML (standard Pipeline, no routing memory)",
         ["rof pipeline run tests/fixtures/pipeline_fakenews_detection/pipeline.yaml --provider anthropic",
          "rof pipeline run tests/fixtures/pipeline_fakenews_detection/pipeline.yaml --provider openai --json"]),
    ]

    for group_title, cmds in cli_cmds:
        subsection(group_title)
        for cmd in cmds:
            cmd_line(cmd)
        blank()

    # Actually execute lint on all six files using subprocess
    subsection("Lint results (executed now against the actual .rl files)")
    blank()

    rof_cli = "rof"
    python  = sys.executable
    all_ok  = True

    for rl_file in STAGE_RL_FILES:
        path = str(HERE / rl_file)
        try:
            proc = subprocess.run(
                [rof_cli, "lint", path],
                capture_output=True, text=True, timeout=15,
            )
            # Strip ANSI codes for clean display
            import re
            clean_out = re.sub(r'\033\[[0-9;]*m', '', proc.stdout).strip()
            ok        = proc.returncode == 0
            all_ok    = all_ok and ok
            marker    = OK("✓") if ok else ERR("✗")
            # Print compact summary (just the pass/fail line)
            summary   = next(
                (l for l in clean_out.splitlines() if "passed" in l or "failed" in l or "issue" in l),
                clean_out.splitlines()[-1] if clean_out else "no output"
            )
            print(f"  {marker}  {rl_file:<28}  {DIM(summary.strip())}")
        except FileNotFoundError:
            print(f"  {WARN('?')}  {rl_file:<28}  {DIM('rof CLI not found — run: pip install -e . (skipped)')}")
        except subprocess.TimeoutExpired:
            print(f"  {WARN('?')}  {rl_file:<28}  {DIM('timeout')}")

    blank()
    if all_ok:
        note("All six .rl files pass lint — ready for pipeline execution.")
    else:
        note("Some lint issues found — check output above.")


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2 — First article: static routing baseline
# ═══════════════════════════════════════════════════════════════════════════════

def section_first_article(memory: RoutingMemory) -> dict:
    section("2 · First Article — Static Routing Baseline (Tier 1 only)")

    article = ARTICLES[0]
    print(f"  Article:  {BOLD(article.id)}  {DIM('|')}  {article.domain}")
    print(f"  Headline: {article.headline}")
    print(f"  Expected: {OK(article.expected_label)}")
    blank()

    note("RoutingMemory is empty. composite confidence = static similarity only.")
    note("hist=0.500 on every trace (neutral prior, reliability=0).")
    blank()

    pipeline = _build_pipeline(article, memory)
    result   = pipeline.run()
    t_list   = _traces(result.final_snapshot)
    rpt      = _report(result.final_snapshot)

    subsection("Routing trace — all 18 decisions across 6 stages")
    print(f"\n  {'stage':<14} {'goal_pattern':<35} {'tool':<22} "
          f"{'static':>7} {'hist':>7} {'comp':>7}  tier")
    print(f"  {'─'*13} {'─'*34} {'─'*21} "
          f"{'─'*7} {'─'*7} {'─'*7}  {'─'*10}")
    for t in t_list:
        tier = DIM(t["tier"])
        comp = VAL(f"{t['composite']:.3f}")
        print(
            f"  {t['stage']:<14} {t['pattern']:<35} {t['tool']:<22} "
            f"{t['static']:>7.3f} {t['hist']:>7.3f} {comp:>7}  {tier}"
        )

    subsection("Fact-check result")
    blank()
    print(f"  Rating:    {BOLD(rpt.get('rating', '?'))}")
    print(f"  Score:     {VAL(str(rpt.get('credibility_pct', '?')))}%")
    print(f"  Evidence:  {DIM(rpt.get('key_evidence', '?'))}")

    tool_routed = [t for t in t_list if t["tool"] != "LLM"]
    blank()
    note(f"Memory now has {len(memory)} entries from {len(tool_routed)} tool-routed decisions.")

    return result.final_snapshot


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3 — Five articles: routing confidence evolution
# ═══════════════════════════════════════════════════════════════════════════════

def section_five_articles(memory: RoutingMemory) -> list[dict]:
    section("3 · Five Articles — Routing Confidence Evolution Across Runs")

    note("The same RoutingMemory accumulates across every article.")
    note("Watch composite confidence shift as historical reliability grows.")
    blank()

    # Print table header once
    print(f"  {'Art':>4}  {'goal_pattern':<34} {'tool':<22} "
          f"{'static':>7} {'hist':>6} {'comp':>7}  tier")
    print(f"  {'─'*4}  {'─'*33} {'─'*21} "
          f"{'─'*7} {'─'*6} {'─'*7}  {'─'*10}")

    snapshots: list[dict] = []
    results_summary: list[tuple] = []

    for art in ARTICLES:
        pipeline  = _build_pipeline(art, memory)
        result    = pipeline.run()
        t_list    = _traces(result.final_snapshot)
        rpt       = _report(result.final_snapshot)
        vrd       = _verdict(result.final_snapshot)

        for t in t_list:
            if t["tool"] == "LLM":
                continue      # focus on tool-routed decisions
            tier_col = TIER(t["tier"]) if t["tier"] != "static" else DIM(t["tier"])
            comp_col = VAL(f"{t['composite']:.3f}")
            print(
                f"  {art.id:>4}  {t['pattern']:<34} {t['tool']:<22} "
                f"{t['static']:>7.3f} {t['hist']:>6.3f} {comp_col:>7}  {tier_col}"
            )

        label    = str(vrd.get("label", "?"))
        correct  = label == art.expected_label
        results_summary.append((art.id, art.headline[:55], label, art.expected_label, correct))
        snapshots.append(result.final_snapshot)

    # Verdict summary
    subsection("Credibility verdicts vs expected labels")
    blank()
    correct_count = 0
    for art_id, headline, label, expected, correct in results_summary:
        mark = OK("✓") if correct else WARN("≈")
        if correct:
            correct_count += 1
        print(f"  {mark}  {art_id}  {headline:<58}  {label}  {DIM('(expected: ' + expected + ')')}")

    blank()
    note(f"Correct: {correct_count}/{len(ARTICLES)} articles labelled as expected.")

    return snapshots


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4 — Routing memory inspector
# ═══════════════════════════════════════════════════════════════════════════════

def section_memory_inspector(memory: RoutingMemory) -> None:
    section("4 · Routing Memory Inspector — Learned Confidence State")

    inspector = RoutingMemoryInspector(memory)
    norm      = GoalPatternNormalizer()

    subsection("Full routing memory table")
    blank()
    print(inspector.summary())

    subsection("Confidence evolution for key patterns")
    blank()

    interesting = [
        ("extract claims from Article",             "ClaimExtractorTool"),
        ("lookup source credibility for SourceInfo","SourceCredibilityTool"),
        ("cross_reference claims in ClaimSet",      "CrossReferenceTool"),
        ("analyze bias patterns in Article",        "BiasDetectorTool"),
        ("score credibility across all signals",    "CredibilityScorerTool"),
        ("generate report for FactCheckReport",     "ReportFormatterTool"),
    ]
    for goal_expr, tool_name in interesting:
        pattern = norm.normalize(goal_expr)
        evo     = inspector.confidence_evolution(pattern, tool_name)
        print(f"  {evo}")
        blank()

    subsection("Best tool recommendation per goal")
    blank()
    queries = [
        "extract claims from Article",
        "identify source information for Article",
        "lookup source credibility for SourceInfo",
        "cross_reference claims in ClaimSet",
        "analyze bias patterns in Article",
        "score credibility across all signals",
        "generate report for FactCheckReport",
    ]
    for goal in queries:
        best   = inspector.best_tool_for(goal)
        marker = OK("✓") if best else WARN("?")
        print(f"  {marker}  {goal:<55}  →  {best or DIM('(no data)')}")


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5 — Routing uncertainty on an ambiguous goal
# ═══════════════════════════════════════════════════════════════════════════════

def section_uncertainty(memory: RoutingMemory) -> None:
    section("5 · Routing Uncertainty — EventBus Integration")

    note("'assess article structure and narrative' has no strong tool keyword match.")
    note("ConfidentOrchestrator flags is_uncertain=True, falls back to LLM.")
    note("We subscribe to routing.uncertain on the EventBus to capture these live.")
    blank()

    uncertain_events: list[dict] = []
    decided_events:   list[dict] = []

    bus = EventBus()
    bus.subscribe("routing.uncertain", lambda e: uncertain_events.append(e.payload))
    bus.subscribe("routing.decided",   lambda e: decided_events.append(e.payload))

    article  = ARTICLES[2]    # city council article — clean, mixes tool + LLM goals
    pipeline = _build_pipeline(article, memory, bus=bus)
    pipeline.run()

    subsection(f"routing.uncertain events for article {article.id}")
    if uncertain_events:
        for ev in uncertain_events:
            print(f"  {WARN('⚠  routing.uncertain')}")
            print(f"     goal:       {ev.get('goal')!r}")
            print(f"     composite:  {ev.get('composite_confidence')}")
            print(f"     threshold:  {ev.get('threshold')}")
            print(f"     pattern:    {ev.get('pattern')!r}")
            blank()
    else:
        print(f"  {DIM('  (no uncertain events in this run — all goals matched tools)')}")
        blank()

    subsection("routing.decided event stream (all tool-routed goals)")
    print(f"\n  {'goal_pattern':<38} {'tool':<22} {'composite':>10}  uncertain")
    print(f"  {'─'*37} {'─'*21} {'─'*10}  {'─'*9}")
    for ev in decided_events:
        u = WARN(" yes ⚠") if ev.get("is_uncertain") else OK(" no")
        comp_str = str(ev.get("composite_confidence", "?"))
        print(
            f"  {ev.get('pattern',''):<38} "
            f"{ev.get('tool','LLM'):<22} "
            f"{comp_str:>10}  {u}"
        )
    blank()


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6 — Memory persistence (save / load cycle)
# ═══════════════════════════════════════════════════════════════════════════════

def section_persistence(memory: RoutingMemory) -> None:
    section("6 · Memory Persistence — Save / Load Across Process Boundaries")

    note("RoutingMemory serialises to any StateAdapter.")
    note("In production: swap InMemoryStateAdapter for Redis or Postgres.")
    blank()

    adapter = InMemoryStateAdapter()
    memory.save(adapter)

    subsection("Saved memory payload (three entries)")
    raw     = adapter.load("__routing_memory__")
    for key in list(raw.keys())[:3]:
        entry = raw[key]
        n     = entry["attempt_count"]
        ema   = entry["ema_confidence"]
        rel   = min(n / 10.0, 1.0)
        print(f"  {DIM(key)}")
        print(f"    attempts={n}  ema={ema:.4f}  reliability={rel:.2f}")
        blank()

    subsection("Load into fresh RoutingMemory (new process simulation)")
    blank()

    memory2 = RoutingMemory()
    loaded  = memory2.load(adapter)
    print(f"  {OK('✓')}  loaded={loaded}  entries_restored={len(memory2)}")

    norm    = GoalPatternNormalizer()
    pattern = norm.normalize("score credibility across all signals")
    conf, rel = memory2.get_historical_confidence(pattern, "CredibilityScorerTool")
    print(f"  {OK('✓')}  CredibilityScorerTool restored:  ema={VAL(f'{conf:.4f}')}  reliability={rel:.2f}")

    subsection("Continuity run on restored memory")
    pipeline = _build_pipeline(ARTICLES[4], memory2)
    result   = pipeline.run()
    t_list   = [t for t in _traces(result.final_snapshot) if t["tool"] != "LLM"]
    print(f"\n  {OK('✓')}  Article {ARTICLES[4].id} processed on restored memory.")
    print(f"  {OK('✓')}  {len(t_list)} tool-routed decisions; historical tier active.\n")
    for t in t_list[:5]:
        tier = TIER(t["tier"]) if t["tier"] != "static" else DIM(t["tier"])
        comp = VAL(f"{t['composite']:.3f}")
        print(f"     {t['pattern']:<38}  composite={comp}  tier={tier}")
    blank()


# ═══════════════════════════════════════════════════════════════════════════════
# Section 7 — Full snapshot audit trail for the last article
# ═══════════════════════════════════════════════════════════════════════════════

def section_audit_trail(memory: RoutingMemory) -> None:
    section("7 · Final Snapshot — Complete Audit Trail")

    note("The snapshot accumulates every entity written across all six stages.")
    note("Business entities and RoutingTrace entities coexist in the same dict.")
    note("This is the immutable, replayable record of the full assessment.")
    blank()

    pipeline = _build_pipeline(ARTICLES[1], memory)   # A002 — the false article
    result   = pipeline.run()
    snap     = result.final_snapshot

    entities = snap.get("entities", {})
    business = {k: v for k, v in entities.items() if not k.startswith("RoutingTrace")}
    routing  = {k: v for k, v in entities.items() if k.startswith("RoutingTrace")}

    subsection(f"Business entities ({len(business)})")
    for name, ent in sorted(business.items()):
        attrs = ent.get("attributes", {})
        preds = ent.get("predicates", [])
        print(f"\n  {H2(name)}")
        for k, v in attrs.items():
            if not str(k).startswith("_"):
                print(f"    {DIM(k+':')} {v}")
        if preds:
            print(f"    {DIM('is:')} {', '.join(preds)}")

    subsection(f"RoutingTrace entities ({len(routing)})")
    blank()
    t_list = _traces(snap)
    for t in t_list:
        uncertain_mark = f"  {WARN('⚠')}" if t["uncertain"] else ""
        tier   = TIER(t["tier"]) if t["tier"] != "static" else DIM(t["tier"])
        comp_v = VAL(f"{t['composite']:.3f}")
        print(
            f"  [{t['stage']:<14}]  {t['pattern']:<35}  "
            f"→ {t['tool']:<22}  "
            f"comp={comp_v}  "
            f"sat={t['sat']:.3f}  tier={tier}"
            + uncertain_mark
        )
    blank()
    note(f"Total entities: {len(entities)}  ({len(business)} business + {len(routing)} routing traces)")

    # Print the fact-check report cleanly
    subsection("Fact-Check Report — Article A002")
    blank()
    rpt = _report(snap)
    vrd = _verdict(snap)
    print(f"  {BOLD('Rating:')}     {BOLD(rpt.get('rating', '?'))}")
    print(f"  {BOLD('Score:')}      {VAL(str(rpt.get('credibility_pct', '?')))}%")
    print(f"  {BOLD('Headline:')}   {rpt.get('headline', '?')[:65]}")
    print(f"  {BOLD('Publisher:')}  {rpt.get('publisher', '?')}")
    print(f"  {BOLD('Evidence:')}   {rpt.get('key_evidence', '?')}")
    blank()


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print()
    print(SEP)
    print(H1("  ROF Showcase: News Credibility Assessment"))
    print(H1("  Learned Routing Confidence · Six-Stage Fact-Check Pipeline"))
    print(SEP)
    blank()

    note("Domain:  Fake-news detection / article credibility scoring")
    note("Stages:  extract → verify_source → cross_reference →")
    note("         bias_analysis → decide → report")
    note("Articles: 5 test items (3 credible, 2 false/misleading)")
    note("Tools:   6 deterministic + LLM fallback for narrative goals")
    blank()

    shared_memory = RoutingMemory()

    section_cli_workflow()
    first_snap   = section_first_article(shared_memory)
    all_snaps    = section_five_articles(shared_memory)
    section_memory_inspector(shared_memory)
    section_uncertainty(shared_memory)
    section_persistence(shared_memory)
    section_audit_trail(shared_memory)

    # ── Final summary ────────────────────────────────────────────────────────
    section("✓ Showcase Complete")

    total_obs = sum(s.attempt_count for s in shared_memory.all_stats())
    print(f"  {DIM('RoutingMemory:')}  {len(shared_memory)} patterns  ·  {total_obs} observations")
    blank()
    print(f"  {DIM('Key takeaways:')}")
    print(f"  • {BOLD('.rl files')} are the deployable spec: lintable, diffable, non-developer-readable.")
    print(f"  • {BOLD('CLI')} lets you develop each stage in isolation (lint → inspect → run → debug).")
    print(f"  • {BOLD('ConfidentPipeline')} is a drop-in upgrade: same YAML topology, same .rl files.")
    print(f"  • Routing learns from {total_obs} observations with zero training data or labels.")
    print(f"  • {BOLD('RoutingTrace')} entities make every routing decision part of the audit trail.")
    print(f"  • Memory persists to any StateAdapter — move to Redis/Postgres with one line.")
    blank()
    print(f"  {DIM('Next steps:')}")
    print(f"  $ export ANTHROPIC_API_KEY=sk-...")
    print(f"  $ rof pipeline run tests/fixtures/pipeline_fakenews_detection/pipeline.yaml --provider anthropic")
    blank()


if __name__ == "__main__":
    main()
