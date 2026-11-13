from my_agent.config.schema import Config


def test_default_paths_are_nimbusops_paths():
    config = Config.model_validate({"agents": {"defaults": {"provider": "custom"}}})

    assert config.agents.defaults.workspace == "~/.nimbusops/workspace"
    assert config.agents.defaults.bot_name == "NimbusOps"
    assert config.gateway.host == "127.0.0.1"
    assert config.gateway.port == 18790


def test_mcp_server_config_parses_camel_case_aliases():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom"}},
            "tools": {
                "mcpServers": {
                    "fs": {
                        "type": "stdio",
                        "command": "node",
                        "args": ["server.js"],
                        "enabledTools": ["read_file", "mcp_fs_stat"],
                        "toolTimeout": 9,
                    }
                }
            },
        }
    )

    server = config.tools.mcp_servers["fs"]
    assert server.type == "stdio"
    assert server.command == "node"
    assert server.args == ["server.js"]
    assert server.enabled_tools == ["read_file", "mcp_fs_stat"]
    assert server.tool_timeout == 9


def test_action_guardrail_required_step_scopes_parse_camel_case():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom"}},
            "guardrails": {
                "action": {
                    "requiredSteps": ["echo"],
                    "requiredStepScopes": [
                        {
                            "scopeType": "session",
                            "scopeId": "websocket:incident",
                            "steps": [" inspect ", "inspect"],
                        },
                        {
                            "scopeType": "automation",
                            "scopeId": "daily-check",
                            "steps": ["echo"],
                        },
                    ],
                }
            },
        }
    )

    action = config.guardrails.action
    assert action.required_steps == ["echo"]
    assert action.required_step_scopes[0].scope_id == "websocket:incident"
    assert action.required_step_scopes[0].steps == ["inspect"]
    assert action.required_step_scopes[1].scope_type == "automation"
