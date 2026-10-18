# SPDX-FileCopyrightText: 2026 Vericto S.A.S.
# SPDX-License-Identifier: Apache-2.0

"""A LangChain SQL agent whose every query is checked by Vericto.

pip install "vericto[langchain]" langchain-classic langchain-openai
"""

from __future__ import annotations

import os

from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
from langchain_openai import ChatOpenAI

from vericto.langchain import VerictoCheckSQLTool, VerictoSQLDatabase

# Same arguments as SQLDatabase.from_uri; the guard comes from VERICTO_* (proxy:evaluate key).
db = VerictoSQLDatabase.from_uri(os.environ.get("DATABASE_URL", "postgresql+psycopg://app@localhost/app"))
llm = ChatOpenAI(model="gpt-4.1-mini")
agent = create_sql_agent(
    llm,
    toolkit=SQLDatabaseToolkit(db=db, llm=llm),
    agent_type="tool-calling",
    extra_tools=[VerictoCheckSQLTool(db=db)],
)
# A blocked query comes back to the model as "Error: Vericto blocked this query (...)
# Suggested fix: ...", so it can rewrite it; nothing blocked reaches the database.
print(agent.invoke({"input": "How many customers signed up last week?"})["output"])
