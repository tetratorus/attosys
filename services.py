def employee_service(root, company, agent):
    home = f'/home/{agent}'
    environment = 'EnvironmentFile=' + company['llm_env_file'] if company.get('llm_env_file') else ''
    return f"""[Unit]
Description={company.get('name', company['org'])} agent: {agent}
After=network.target

[Service]
Type=simple
User={agent}
Group={agent}
UMask=0027
WorkingDirectory={home}
{environment}
ExecStart={root}/venv/bin/python {root}/harness/agent.py {home}/agent {home}/subconscious
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
