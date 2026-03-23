// rof_knowledge_qa.rl
// ──────────────────────────────────────────────────────────────────────────────
// RAGTool showcase: knowledge-base Q&A over ROF framework documentation.
//
// Scenario
// --------
// A developer is onboarding to the ROF framework and wants to query a
// pre-loaded knowledge base to answer four questions:
//
//   Q1. How does ToolRouter decide which tool to call?
//   Q2. What backends does RAGTool support?
//   Q3. How do you define a pipeline in ROF?
//   Q4. What is the RelateLang .rl syntax and how are goals written?
//
// How it works
// ------------
// The RAGTool trigger phrase "retrieve information about <topic>" routes
// each goal to RAGTool.  The tool performs a cosine-similarity search over
// its in-memory vector store and returns the top-k most relevant document
// chunks.  Those chunks are written into the WorkflowGraph as KnowledgeDoc
// entities, which the LLM then synthesises into a grounded Answer entity.
//
// Trigger phrase: "retrieve information about <topic> from the knowledge base"
// Tool:           RAGTool  (in_memory backend — zero extra dependencies)
// Documents:      Loaded programmatically by the runner script before .rl runs
// ──────────────────────────────────────────────────────────────────────────────

// ── Knowledge base configuration ─────────────────────────────────────────────

define KnowledgeBase as "An in-memory vector store pre-loaded with ROF framework documentation chunks".
define Corpus as "The collection of ROF documentation passages used as retrieval source".

KnowledgeBase has backend of "in_memory".
KnowledgeBase has collection of "rof_docs".
KnowledgeBase has description of "ROF framework docs: tools, routing, pipelines, RelateLang syntax".
KnowledgeBase has top_k of 3.

Corpus has source of "ROF framework internal documentation".
Corpus has chunk_count of 20.
Corpus has topics of "ToolRouter, RAGTool, DatabaseTool, pipeline YAML, RelateLang syntax, goals, entities, conditions".

relate Corpus and KnowledgeBase as "populates".

// ── Developer persona ─────────────────────────────────────────────────────────

define Developer as "A software engineer onboarding to the ROF framework".

Developer has role of "backend engineer".
Developer has experience_level of "intermediate".
Developer has goal of "understand how to use ROF tools and write .rl scripts".

relate Developer and KnowledgeBase as "queries".

// ── Q1: Tool routing ──────────────────────────────────────────────────────────

define RoutingQuery as "Natural language question about how ToolRouter selects a tool".
define RoutingAnswer as "A grounded explanation of the ToolRouter decision process".

RoutingQuery has text of "How does ToolRouter decide which tool to call?".
RoutingQuery has top_k of 3.

relate RoutingQuery and KnowledgeBase as "searches".
relate KnowledgeBase and RoutingAnswer as "grounds".
relate Developer and RoutingAnswer as "receives".

// ── Q2: RAGTool backends ──────────────────────────────────────────────────────

define RAGBackendQuery as "Question about which storage backends RAGTool supports".
define RAGBackendAnswer as "Summary of RAGTool backend options and their dependencies".

RAGBackendQuery has text of "What backends does RAGTool support and when should I use each?".
RAGBackendQuery has top_k of 3.

relate RAGBackendQuery and KnowledgeBase as "searches".
relate KnowledgeBase and RAGBackendAnswer as "grounds".
relate Developer and RAGBackendAnswer as "receives".

// ── Q3: Pipeline definition ───────────────────────────────────────────────────

define PipelineQuery as "Question about how to define a multi-stage ROF pipeline".
define PipelineAnswer as "A grounded explanation of YAML pipeline configuration in ROF".

PipelineQuery has text of "How do you define and run a multi-stage pipeline in ROF?".
PipelineQuery has top_k of 3.

relate PipelineQuery and KnowledgeBase as "searches".
relate KnowledgeBase and PipelineAnswer as "grounds".
relate Developer and PipelineAnswer as "receives".

// ── Q4: RelateLang syntax ─────────────────────────────────────────────────────

define SyntaxQuery as "Question about the RelateLang .rl file format and goal syntax".
define SyntaxAnswer as "A concise explanation of RelateLang entities, attributes, conditions and goals".

SyntaxQuery has text of "What is the RelateLang .rl syntax and how are goals written?".
SyntaxQuery has top_k of 4.

relate SyntaxQuery and KnowledgeBase as "searches".
relate KnowledgeBase and SyntaxAnswer as "grounds".
relate Developer and SyntaxAnswer as "receives".

// ── Synthesised onboarding guide ──────────────────────────────────────────────

define OnboardingGuide as "A consolidated getting-started guide assembled from all four Q&A pairs".

OnboardingGuide has format of "structured".
OnboardingGuide has audience of "new ROF developer".

relate RoutingAnswer and OnboardingGuide as "contributes to".
relate RAGBackendAnswer and OnboardingGuide as "contributes to".
relate PipelineAnswer and OnboardingGuide as "contributes to".
relate SyntaxAnswer and OnboardingGuide as "contributes to".
relate Developer and OnboardingGuide as "benefits from".

// ── Goals ─────────────────────────────────────────────────────────────────────
// Each goal triggers RAGTool via "retrieve information about … from the knowledge base".
//
// Goal verb note (§2.7.3):
//   "retrieve information about" is a tool-trigger phrase (RAGTool keyword);
//   the output modality is implicitly natural language / structured knowledge.
//   "produce … summary_sections" and "produce … next_learning_steps" use the
//   recommended verb "produce" with an explicit output entity per §2.7.1.

ensure retrieve information about ToolRouter routing strategy from the knowledge base.
ensure retrieve information about RAGTool backends and vector store options from the knowledge base.
ensure retrieve information about pipeline definition and multi-stage orchestration from the knowledge base.
ensure retrieve information about RelateLang syntax entities attributes conditions and goals from the knowledge base.
ensure produce OnboardingGuide summary_sections based on all retrieved answers.
ensure produce next_learning_steps for Developer based on OnboardingGuide.
