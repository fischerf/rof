// rag_retrieval.rl
// Demonstrates RAGTool: retrieve relevant documents from a vector knowledge base.
// Trigger phrase: "retrieve information about <topic>" / "knowledge base lookup"

define KnowledgeBase as "A vector store of ROF framework documentation chunks".
define Query as "The natural language query issued against the knowledge base".
define RetrievedContext as "Top-k document chunks most relevant to the query".
define Answer as "A synthesised response grounded in the retrieved context".

KnowledgeBase has backend of "in_memory".
KnowledgeBase has description of "ROF framework docs: tools, routing, orchestration".

Query has text of "How does ToolRouter decide which tool to call?".
Query has top_k of 3.

RetrievedContext has min_relevance of 0.4.

relate Query and KnowledgeBase as "searches".
relate KnowledgeBase and RetrievedContext as "returns".
relate RetrievedContext and Answer as "grounds".

ensure retrieve information about tool routing strategy from the knowledge base.
ensure determine Answer grounded_response.
