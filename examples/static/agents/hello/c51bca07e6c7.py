"""A minimal rapp-mcp agent. Drop any *_agent.py like this into your agents folder."""
try:
    from agents.basic_agent import BasicAgent
except ImportError:
    from basic_agent import BasicAgent


class HelloAgent(BasicAgent):
    def __init__(self):
        self.name = "hello"
        self.metadata = {
            "name": self.name,
            "description": "Say hello to someone.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
                "required": ["name"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        return f"Hello, {kwargs.get('name', 'world')}!"
