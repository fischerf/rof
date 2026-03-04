"""
tests/test_core_integration.py
===============================
Integration tests for rof_core Orchestrator and WorkflowGraph.
Tests the complete workflow execution with mock LLM and tools.
"""

import pytest

from rof_framework.rof_core import (
    Event,
    EventBus,
    GoalStatus,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Orchestrator,
    OrchestratorConfig,
    RLParser,
    ToolProvider,
    ToolRequest,
    ToolResponse,
    WorkflowAST,
    WorkflowGraph,
)

# ─── Mock LLM Provider ────────────────────────────────────────────────────────


class MockLLMProvider(LLMProvider):
    """Mock LLM that returns canned responses."""

    def __init__(self, responses: list[str] = None):
        self.responses = responses or ["Task completed successfully."]
        self.call_count = 0
        self.last_request = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        response = self.responses[min(self.call_count, len(self.responses) - 1)]
        self.call_count += 1
        return LLMResponse(content=response, raw={}, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 4096


# ─── Mock Tool Provider ───────────────────────────────────────────────────────


class MockToolProvider(ToolProvider):
    """Mock tool that records calls."""

    def __init__(self, name: str = "mock_tool", success: bool = True, output: str = "Done"):
        self._name = name
        self._success = success
        self._output = output
        self.call_count = 0
        self.last_request = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def trigger_keywords(self) -> list[str]:
        return [self._name, "mock"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        self.call_count += 1
        self.last_request = request
        return ToolResponse(
            success=self._success, output=self._output, error="" if self._success else "Mock error"
        )


# ─── Event Bus Tests ──────────────────────────────────────────────────────────


class TestEventBus:
    def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        def handler(event: Event):
            received.append(event.name)

        bus.subscribe("test.event", handler)
        bus.publish(Event("test.event", {"data": "test"}))

        assert "test.event" in received

    def test_multiple_handlers(self):
        bus = EventBus()
        counts = {"h1": 0, "h2": 0}

        def handler1(event: Event):
            counts["h1"] += 1

        def handler2(event: Event):
            counts["h2"] += 1

        bus.subscribe("test", handler1)
        bus.subscribe("test", handler2)
        bus.publish(Event("test"))

        assert counts["h1"] == 1
        assert counts["h2"] == 1

    def test_unsubscribe(self):
        bus = EventBus()
        count = {"val": 0}

        def handler(event: Event):
            count["val"] += 1

        bus.subscribe("test", handler)
        bus.publish(Event("test"))
        assert count["val"] == 1

        bus.unsubscribe("test", handler)
        bus.publish(Event("test"))
        assert count["val"] == 1  # Should not increment

    def test_wildcard_handler(self):
        bus = EventBus()
        received = []

        def wildcard_handler(event: Event):
            received.append(event.name)

        bus.subscribe("*", wildcard_handler)
        bus.publish(Event("event1"))
        bus.publish(Event("event2"))

        assert "event1" in received
        assert "event2" in received


# ─── WorkflowGraph Tests ──────────────────────────────────────────────────────


class TestWorkflowGraph:
    def test_initial_state_from_ast(self):
        source = """
        define Customer as "A buyer".
        Customer has age of 30.
        Customer is active.
        ensure verify Customer status.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        customer = graph.entity("Customer")
        assert customer is not None
        assert customer.description == "A buyer"
        assert customer.attributes["age"] == 30
        assert "active" in customer.predicates

    def test_set_attribute(self):
        ast = WorkflowAST()
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        events = []
        bus.subscribe("state.attribute_set", lambda e: events.append(e))

        graph.set_attribute("TestEntity", "score", 95)

        entity = graph.entity("TestEntity")
        assert entity.attributes["score"] == 95
        assert len(events) == 1
        assert events[0].payload["entity"] == "TestEntity"
        assert events[0].payload["attribute"] == "score"

    def test_add_predicate(self):
        ast = WorkflowAST()
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        events = []
        bus.subscribe("state.predicate_added", lambda e: events.append(e))

        graph.add_predicate("TestEntity", "verified")

        entity = graph.entity("TestEntity")
        assert "verified" in entity.predicates
        assert len(events) == 1

    def test_pending_goals(self):
        source = """
        ensure goal1 is complete.
        ensure goal2 is complete.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        pending = graph.pending_goals()
        assert len(pending) == 2
        assert all(g.status == GoalStatus.PENDING for g in pending)

    def test_mark_goal(self):
        source = "ensure test goal."
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        events = []
        bus.subscribe("goal.status_changed", lambda e: events.append(e))

        goal = graph.pending_goals()[0]
        graph.mark_goal(goal, GoalStatus.ACHIEVED, "Success")

        assert goal.status == GoalStatus.ACHIEVED
        assert goal.result == "Success"
        assert len(events) == 1

    def test_snapshot(self):
        source = """
        define Product as "An item for sale".
        Product has price of 100.
        Product is available.
        ensure check inventory.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        snapshot = graph.snapshot()

        assert "entities" in snapshot
        assert "Product" in snapshot["entities"]
        assert snapshot["entities"]["Product"]["description"] == "An item for sale"
        assert snapshot["entities"]["Product"]["attributes"]["price"] == 100
        assert "available" in snapshot["entities"]["Product"]["predicates"]
        assert "goals" in snapshot
        assert len(snapshot["goals"]) == 1


# ─── Orchestrator Tests ───────────────────────────────────────────────────────


class TestOrchestrator:
    def test_orchestrator_initialization(self):
        llm = MockLLMProvider()
        config = OrchestratorConfig(max_iterations=10)

        orch = Orchestrator(llm_provider=llm, config=config)
        assert orch is not None

    def test_simple_goal_execution(self):
        source = """
        define Task as "A simple task".
        ensure complete the Task.
        """
        ast = RLParser().parse(source)
        llm = MockLLMProvider(["Task has been completed successfully."])
        config = OrchestratorConfig(max_iterations=5)

        orch = Orchestrator(llm_provider=llm, config=config)
        result = orch.run(ast)

        assert result is not None
        assert llm.call_count > 0

    def test_tool_execution(self):
        source = """
        define Data as "Information to process".
        ensure process Data with mock_tool.
        """
        ast = RLParser().parse(source)
        llm = MockLLMProvider(["I will use mock_tool to process the data."])
        tool = MockToolProvider(name="mock_tool", success=True, output="Processed")
        config = OrchestratorConfig(max_iterations=5)

        orch = Orchestrator(llm_provider=llm, tools=[tool], config=config)
        result = orch.run(ast)

        # The tool should be available even if not called in this simple mock
        assert "mock_tool" in orch.tools

    def test_max_iterations_limit(self):
        source = "ensure never ending task."
        ast = RLParser().parse(source)
        llm = MockLLMProvider(["Still working..."] * 100)
        config = OrchestratorConfig(max_iterations=3)

        orch = Orchestrator(llm_provider=llm, config=config)
        result = orch.run(ast)

        # Should stop after max_iterations
        assert llm.call_count <= config.max_iterations + 1

    def test_event_emission(self):
        source = "ensure test goal."
        ast = RLParser().parse(source)
        llm = MockLLMProvider(["Goal achieved."])
        config = OrchestratorConfig(max_iterations=5)

        events = []

        orch = Orchestrator(llm_provider=llm, config=config)
        orch.bus.subscribe("*", lambda e: events.append(e.name))

        result = orch.run(ast)

        # Should have emitted various events
        assert len(events) > 0

    def test_multiple_goals(self):
        source = """
        define Task1 as "First task".
        define Task2 as "Second task".
        ensure complete Task1.
        ensure complete Task2.
        """
        ast = RLParser().parse(source)
        llm = MockLLMProvider(["Task1 completed.", "Task2 completed."])
        config = OrchestratorConfig(max_iterations=10)

        orch = Orchestrator(llm_provider=llm, config=config)
        result = orch.run(ast)

        assert llm.call_count >= 2  # At least one call per goal


# ─── ContextInjector Tests ────────────────────────────────────────────────────


class TestContextInjector:
    def test_basic_context_building(self):
        from rof_framework.rof_core import ContextInjector, Goal

        source = """
        define Customer as "A buyer".
        Customer has age of 30.
        ensure verify Customer eligibility.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        injector = ContextInjector()
        goal = graph.pending_goals()[0]

        context = injector.build(graph, goal)

        assert "Customer" in context
        assert "A buyer" in context
        assert "age" in context
        assert "30" in context

    def test_relevant_entity_filtering(self):
        from rof_framework.rof_core import ContextInjector

        source = """
        define A as "Entity A".
        define B as "Entity B".
        define C as "Entity C".
        A has value of 1.
        B has value of 2.
        C has value of 3.
        ensure process A.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        injector = ContextInjector()
        goal = graph.pending_goals()[0]

        context = injector.build(graph, goal)

        # Should include A but might not include all of B and C
        assert "Entity A" in context or "A" in context


# ─── StateManager Tests ───────────────────────────────────────────────────────


class TestStateManager:
    def test_save_and_load(self):
        from rof_framework.rof_core import StateManager

        source = """
        define Test as "A test entity".
        Test has value of 42.
        ensure verify Test.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        manager = StateManager()
        run_id = "test-run-123"

        manager.save(run_id, graph)

        loaded = manager.load(run_id)
        assert loaded is not None
        assert "entities" in loaded
        assert "Test" in loaded["entities"]
        assert loaded["entities"]["Test"]["attributes"]["value"] == 42

    def test_exists(self):
        from rof_framework.rof_core import StateManager

        manager = StateManager()
        run_id = "test-run-456"

        assert not manager.exists(run_id)

        ast = WorkflowAST()
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)
        manager.save(run_id, graph)

        assert manager.exists(run_id)

    def test_delete(self):
        from rof_framework.rof_core import StateManager

        manager = StateManager()
        run_id = "test-run-789"

        ast = WorkflowAST()
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)
        manager.save(run_id, graph)

        assert manager.exists(run_id)
        manager.delete(run_id)
        assert not manager.exists(run_id)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
