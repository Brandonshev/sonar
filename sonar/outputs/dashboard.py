"""output.dashboard: the dashboard is a long-running web service (sonar/web.py, sonar-web.service)
that reads the database directly, so there is nothing to do per pipeline run. This node exists so
the workflow map shows where scores go, and so its settings (tabs, port) live in pipeline.json."""


def run(conn, node):
    return 0
