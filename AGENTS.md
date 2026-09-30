# Local runtime

- Install the chat renderer for host-side tests with `python3 -m pip install -r local/requirements.txt`; tests also require aiohttp, requests, and PyYAML.
- Run `python3 -m unittest discover -s local -p 'test_*.py' -v`. The real-container lifecycle test is opt-in, as documented in README.md.
- Local chat runs with `/opt/attosys/venv/bin/python`. Its Markdown renderer disables raw HTML and images; keep these protections when changing rendering.
- `local/up.py start --provider deepseek --model deepseek-flash` configures a new company for DeepSeek. Existing companies retain their saved provider/model settings.
- `--duration 0` explicitly disables the employee shutdown timer; the default remains 900 seconds.
- Source checkouts are copied into the image, not mounted. Rebuild for new instances; a running instance does not automatically pick up host edits.
- If guest DNS fails with host loopback-only resolvers, use an explicitly approved resolver via `--dns` when creating a container. That flag does not change DNS for an existing container.
