# Runs the ACEL demo MCP server (examples/toy_server.py) over stdio.
#
# This is a demonstration server: 5 tools (authenticate, read_user_data,
# validate_record, delete_record, send_payment), 3 ACEL contracts enforced
# live via ACELMiddleware. It exists so ACEL's live-enforcement behavior can
# be inspected by any MCP client/introspection tool — ACEL itself is
# middleware, not a standalone server; this is the reference server it wraps.
#
# Build:  docker build -t acel-demo-server .
# Run:    docker run -i acel-demo-server

FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -e ".[mcp]"

CMD ["python", "examples/toy_server.py"]
