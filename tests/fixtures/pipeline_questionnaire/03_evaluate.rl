// ─────────────────────────────────────────────────────────────────────────────
// 03_evaluate.rl  –  Stage 3: LLM Evaluation of the Human
// ─────────────────────────────────────────────────────────────────────────────
// inject_prior_context: true  →  the full accumulated snapshot from Stages 1+2
// arrives here: Questionnaire.topic/difficulty + all HumanResponses.q_* answers.
//
// The LLM evaluates the answer set and responds with RelateLang:
//
//   Evaluation has score of <0-100>.
//   Evaluation has level of "<beginner|intermediate|advanced|expert>".
//   Evaluation has strengths of "<prose>".
//   Evaluation has gaps of "<prose>".
//   Evaluation has recommendations of "<prose>".
//   HumanRespondent is assessed.
//
// Snapshot produced by this stage:
//   Evaluation.score / level / strengths / gaps / recommendations

define HumanRespondent  as "The human who answered the questionnaire".
define HumanResponses   as "The answers provided by the human during the questionnaire".
define Questionnaire    as "An interactive CLI questionnaire that probes the respondent's knowledge on a topic".
define Evaluation       as "The LLM's structured assessment of the human respondent's knowledge and skill level".

if HumanResponses has answer_count > 0,
    then ensure HumanResponses is ready_for_evaluation.

ensure evaluate HumanResponses against Questionnaire topic and produce Evaluation score out of 100.
ensure determine Evaluation level from score as beginner or intermediate or advanced or expert.
ensure identify Evaluation strengths demonstrated in HumanRespondent answers.
ensure identify Evaluation gaps or misconceptions in HumanRespondent answers.
ensure produce Evaluation recommendations as concrete next learning steps for HumanRespondent.
