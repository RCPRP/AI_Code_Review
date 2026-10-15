1. The PR and the codebase (sample_prs.py, toy_repository.py). The toy repo is a small FastAPI app with team rules in docs/CONVENTIONS.md, for example “every endpoint needs get_current_user”. Each mock PR is a real git repo: main is the clean app, and feature adds code with 2 planted bugs.

2. Finding relevant context (context_engine.py). Every function and class in the repo becomes its own chunk, using Python’s ast module. Each chunk is turned into an embedding vector (with Gemini) and stored in ChromaDB. For a PR, the engine searches with the title, ticket and diff, and returns the 15 most similar chunks plus the conventions doc. That’s how the agent “knows” the codebase without reading all of it.

3. The reviewer agent (session.py). This is a Deep Agents agent. Its first message holds the diff, the ticket and the retrieved code. It also has tools to look further on its own (tools.py): git_diff, read_file and grep. They’re all read-only, and .env and key files are blocked.

It can hand work to two specialist subagents: a security reviewer with an OWASP checklist, and a pattern reviewer that checks the conventions. When it’s done, it calls submit_review, which records typed findings (schema.py: severity, file, line, fix). Then it replies in plain language.

4. Conversation. Every turn shares one memory thread (LangGraph’s checkpointer), so “show me the fix” knows which review you mean.

5. The UI (ui.py + ui.html). This is a FastAPI app plus one HTML page. For the demo, a review opens from its recording in recordings/, which are real Gemini runs I saved. Any question you type is answered live by Gemini, using the diff, the retrieved code and the review.

6. Proof that context helps (reviewer.py, evaluation.py). The benchmark reviews each PR twice, once with only the diff and once with context. Each finding is checked against the planted bugs: it must name the same file and use one of that bug’s keywords. That’s where 70% vs 87% comes from.

Which part should I go deeper on? I can walk through the code of one piece, draw an architecture diagram, or explain the Deep Agents pieces (subagents, tools, memory) in course terms.
