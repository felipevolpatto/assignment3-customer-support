# DESIGN

> Copy this file to `DESIGN.md` and answer it **before you write code**. Revise it as you
> learn, but keep the first version in git history. A stranger grades it (`G-DESIGN`), so
> write for someone who has never seen your code. The five headings are fixed; keep them.

## Components

The system is seven processes plus one external service. My run script starts them in this
order. That order is only a startup sequence. The only direct dependency is the Toolbox on
PostgreSQL: the Toolbox cannot serve tools until the database is up. The Judge, the Masker,
Phoenix and the web app do not depend on each other to boot.

| Component | Port | Separate process? | What it does |
|---|---|---|---|
| PostgreSQL | 5432 | yes | Holds users, orders and the action log. At runtime, database access only occurs through the Toolbox. The seed and the eval reset also run SQL, outside a customer turn. |
| MCP Toolbox | 5000 | yes | Turns the SQL in `tools.yaml` into tools the agent can call over MCP. |
| Security Judge | 10002 | yes (A2A) | Reads every message and returns `allow` or `block` with a reason. |
| Data Masker | 10003 | yes (A2A) | Hides other people's emails, phones and card numbers in the reply. |
| Phoenix | 6006 | yes | Stores one trace per turn, so I can see every step afterwards. |
| Web app (FastAPI) | 8000 | yes | Serves the page and streams the pipeline's events to the browser. |
| CLI | none | yes, while I use it | The terminal front end. Same events as the web app. |
| Mem0 | none (cloud) | external | Stores what each customer told us, searched before every turn. |

The pipeline, the Sanitizer, the Guardrail and the support agent are not processes. They are
code inside the CLI or the web app, in one shared module, so both front ends run the same
logic.

**Why the Judge and Masker are separate but the Guardrail is not.** The Judge and Masker
answer questions that are the same for any product: "is this an attack?" and "does this
leak personal data?". In a real company a security team would own them, and any agent could
call them, so they live behind A2A and can be updated or audited without redeploying the
shop. The Guardrail answers a question only this shop can answer: "is this about orders,
deliveries, returns or the customer's account?". That rule changes when the product changes,
so it belongs inside the product.

## Responsibilities

**Who decides a customer only sees their own orders.** The database query does, not the
agent. In `mcp_toolbox/tools.yaml`, `get-order-status` runs
`WHERE order_id = $1 AND customer_email = $2`, and `find-customer-orders` filters on
`customer_email` the same way. The email is bound to the tools from the session of the
current request, not from anything the model types. On the web server that session is the
login established by `POST /api/login` for this request's `user_id`. The model never sees
the email parameter and cannot pass a different one. If Alice asks about order 5, the SQL
finds no row. The model has nothing to leak, whatever the prompt says.

`action-log` checks ownership too. Before it inserts, it accepts an `order_id` only when
that order belongs to the same session email (`INSERT … SELECT … WHERE`, or a check query
first). An order Alice does not own is not logged.

**Who may hold an API key.** Only server-side Python processes: the pipeline (Gemini and
Mem0 keys), the Judge (Gemini) and the Masker if it calls a model. They read the keys from
`.env`. The browser, the event stream and the traces never contain a key.

**Who decides a turn is over budget.** The pipeline. It counts tool calls, tokens and
wall-clock time on every turn and stops at `T-BUD-TOOLS`, `T-BUD-TOKENS` and `T-BUD-WALL`,
ending with `terminated: "cap"`. The model is never asked to stop itself.

| Rule | Enforced in | Why there |
|---|---|---|
| R-1 Ownership in SQL, email from the session | code | A prompt is a request; the SQL `WHERE` is a wall. This is the rule that failed in the reference build. |
| R-2 Every-turn work is a pipeline step | code | Recall and save are pipeline steps, not tools the model may skip. Recall runs only if the guards let the turn continue. Save runs only after the Masker, and never on a blocked or error turn. |
| R-3 No mutating tool in the agent's toolset | code | `update-order-status` is not in the toolset the agent loads. A tool the agent doesn't have can't be called. |
| R-4 A guard that errors fails the turn | code | The pipeline turns a timeout, a refused connection or an unparseable verdict into an `error` event. Nothing defaults to "safe". |
| R-5 Save only what the user said | code | The save step receives only the user's message. The reply is never passed to Mem0. |
| R-6 Guardrail scope uses this shop's examples | prompt | Deciding "is this about orders?" is a judgement, so it lives in the Guardrail's prompt, with examples like "leave my packages at the back door". It's measured by the legit and off-topic sets. |
| R-7 Reject over-long memories | code | The recall step skips any memory longer than `T-MEM-MAXCHARS` before building the prompt. |
| R-8 A guard changes only what it exists to change | code | The Masker replaces phone numbers, card-like numbers, and emails that are not the logged-in user's own. It leaves that user's email in place, and it does not change case, whitespace or anything else. A test compares the text before and after, character by character. |
| R-9 Explicit verdict | both | The Judge's prompt asks for `{verdict, reason}`, and the pipeline's parser rejects anything else. The prompt alone could be ignored; the parser can't. |
| R-10 Traces outlive the process | code | Phoenix is a separate process with disk storage, started by the run script. |
| R-11 Enumerations in the schema | both | A `CHECK` constraint on `actions_log.action_type` rejects bad values. The tool description lists the five values so the model picks a valid one first. |
| Grounded or nothing | both | The instruction says order facts come from tools. The eval checks that every order fact in a reply came from a tool call in that turn. |
| Bounded and honest | code | The pipeline enforces the caps, and a capped turn says so in the reply. |
| Evidence over vibes | code | Every graded number is written by the eval runner into `reports/eval.json`, never typed by hand. |

The pattern: a rule that must **never** fail goes in code. A prompt is only used where the
decision needs judgement (R-6), or as a first hint backed by a code check (R-9, R-11).

## Communication

A message never goes straight from the user to the database. Both front ends call the same
pipeline, and the pipeline is the only thing that talks to the guards, the memory and the
tools.

**From the CLI.** The CLI calls `pipeline.run()` as a Python function and prints each event
as it is yielded. No HTTP between the CLI and the pipeline: they are the same process.

**From the browser.** The page logs in with `POST /api/login` (HTTP, JSON). A chat message
is `POST /api/chat`, and the reply is a stream of `application/x-ndjson`: one JSON event per
line, sent as each step finishes, not after the whole turn. The web process calls the same
`pipeline.run()` function the CLI does.

**Inside one approved turn, in order.** The table is the full path of a turn that passes
every guard. A block or an error stops the turn there: the hops after the failing step do
not run, including recall, the agent, the Masker and save.

| Hop | Protocol | What crosses it |
|---|---|---|
| CLI or web handler → pipeline | function call | the logged-in email and the message |
| pipeline → Sanitizer | function call | the raw text |
| pipeline → Judge (:10002) | A2A, JSON-RPC over HTTP | the message |
| pipeline → Guardrail | function call, in-process ADK | the message |
| pipeline → Mem0 | HTTPS to the Mem0 cloud API | the message, filtered to this user's email |
| pipeline → support agent | function call (ADK `Runner`) | the message plus the memories that passed the cutoff |
| agent → MCP Toolbox (:5000) | MCP, via `toolbox-core` | a tool name and its arguments |
| Toolbox → PostgreSQL (:5432) | SQL | the statement in `tools.yaml`, with the session email already bound |
| pipeline → Masker (:10003) | A2A, JSON-RPC over HTTP | the agent's final text |
| pipeline → Mem0 | HTTPS | only the user's message, and only if the turn was not blocked |
| pipeline → Phoenix (:6006) | OTLP HTTP | one span per step, under a single `agent.turn` |
| pipeline → CLI | the function yields events | one line printed per step |
| pipeline → browser | NDJSON over the open HTTP response | the same events |

**The A2A method.** I use `message/send`, the current A2A method (SPEC J-1). The older draft
method `tasks/send` is what the course's reference project uses; I am not copying it, because
the spec names `message/send` as the current one. Both the Judge and the Masker expose an
agent card at `/.well-known/agent.json`, and the pipeline reads that card to learn the URL
rather than importing their code.

**What a verdict looks like on the wire.** The Judge returns a JSON object, never the input
echoed back:

    { "verdict": "allow", "reason": "no injection patterns" }

or `{ "verdict": "block", "reason": "SQL injection pattern" }`. Anything else — the original
message, empty text, a timeout, a connection refused — is an error and ends the turn. The
Masker returns the masked text plus a count of what it changed, for example
`{ "text": "…", "masked": { "phone": 1 } }`, or `"nothing to mask"` when it found none.

## State

Seven kinds of state are stored. The two sessions live only in process memory and are
thrown away on purpose when the process restarts.

| What | Where | How long | Customer data? | Who can read it |
|---|---|---|---|---|
| Users, orders | PostgreSQL `users`, `customer_orders` | Until the reset script runs | Yes: name, email, address, what they paid | Only the Toolbox's database user, through the three tools. The agent never runs SQL. |
| Action log | PostgreSQL `actions_log` | Until the reset script runs | Yes: the user's email and what they asked to change | The same database user. The agent can insert a row; it has no tool that reads the log back. |
| Login session | An in-memory map in the web process, keyed by email | Until logout or until the web process restarts | The email only | The web process, to accept or reject `POST /api/chat`. Lost on restart, which is fine: the user logs in again. |
| Agent session | ADK `InMemorySessionService`, one per logged-in user | The life of that process | The turns of this conversation | That process only. The Guardrail does not use it: it opens a fresh session per check and deletes it, so one verdict cannot colour the next. |
| Memories | Mem0 cloud, keyed by the user's email | Until Mem0 deletes them. A save returns `PENDING`. The eval waits at least 120 seconds (`T-MEM-WAIT`) before the follow-up; that wait does not guarantee the memory is ready at exactly 120 seconds. | Yes: what the customer said | The pipeline, using the Mem0 key. A recall is filtered to that email, so Alice's search does not return Bob's memories. |
| Traces | Phoenix on disk, its own process | Across agent restarts. Wiped only if Phoenix's storage is wiped. | Yes: the message and the reply are span attributes | Whoever can open `localhost:6006`. It is not exposed beyond the machine. |
| Run logs | `runs/<turn_id>.json` on local disk | Until deleted. `runs/` is gitignored, except `runs/failing/`, which is submitted. | Yes: the message and the reply | Whoever has the repo checkout. |

The seed stores passwords in plain text. That is demo data, not a pattern I will copy. No password is logged, traced or sent to a model.

**When an order changes after a memory was saved.** Memory is not updated. If Alice cancels order 4, Mem0 can still hold "order 4 is processing" from an earlier turn. The database is the fact; the memory is a recollection. The agent's instruction says to trust a tool result from this turn over any memory, and the eval checks that an order fact in the reply came from a tool call in that turn. I am not deleting or rewriting memories when an order changes, because Mem0 is not the order system. The cost is a stale sentence sitting in the prompt. The mitigation is the SQL, not a promise that the memory is current.

## Trade-offs

The second run measured these times. A blocked attack finished in 2.3 seconds, inside the 5 second limit. A typical order answer took 31 seconds, and the slow tail took 64 seconds, both past the 8 and 15 second limits. On one order the Judge alone took 13 seconds. The run used gemini-3.1-flash-lite because the free quota for the faster model had run out. I am not loosening the limits.

**What each guard costs, and whether it is worth it.**

| Guard | What I expect it to cost | Worth it? |
|---|---|---|
| Sanitizer | No model call. A few milliseconds. | Yes. It is the cheap block for what the length limit and the character allow-list catch. The Judge's deterministic detector is the other cheap block: an obvious attack (`' OR 1=1`, `<script>`, a path traversal) stops there without a Gemini call. Both keep a blocked attack inside `T-LAT-BLOCK-P95` (5 000 ms). |
| Judge | An A2A round trip on every message that passes the Sanitizer. Inside, the deterministic detector runs first; Gemini is called only when the detector does not block. Likely the largest single cost on a passing turn. | Yes. It is the layer that catches injection the allow-list cannot. One measured Judge call took 13 seconds. |
| Guardrail | A Gemini call on every message that passes the Judge. | Yes, because the Judge does not know what this shop is for. The reference build blocked "leave packages at the back door" and let a poem through. That is the failure this layer exists to avoid. |
| Masker | An A2A round trip after the support agent answers, only on turns that reached the agent. A model call only if I build the Masker with one; pattern matching may be enough. | Yes. A leak of someone else's phone number is not recoverable by being fast. |

Before the support agent starts, a passing turn pays for up to two guard model calls: the
Judge's and the Guardrail's. The agent then makes its own calls, and the Masker adds one more
hop after it. `T-LAT-P50` is 8 000 ms and `T-LAT-P95` is 15 000 ms, so there is little room.
If the measured p95 is over 15 000 ms, the fix is to make a guard cheaper, not to edit the
threshold.

**The Judge's detector may block alone.** A pattern match blocks without waiting for Gemini, but only on a payload a real customer of this shop would not send: a SQL fragment such as `' OR 1=1`, a `<script>` tag, a path traversal, or a prompt-extraction phrase. Words the legitimate set actually uses — "select", "drop", "remember that I…", apostrophes — are not enough on their own. Those stay for the model to read. An obvious attack then skips a model call and stays inside `T-LAT-BLOCK-P95` (5 000 ms). Confirming every block with Gemini would make even `' OR 1=1` wait on a model, which spends the budget the guard exists to protect. The false-positive risk is real if a pattern is lazy, so the patterns stay specific and `T-LEGIT-FALSE-BLOCK` (at most 0.05) is what tells me one was too broad.

**The Guardrail fails closed.** An unparseable decision, a timeout, or Gemini being slow
ends the turn with an `error` event. It does not default to "safe". The cost is that a
Gemini outage refuses real customers, and those turns count toward `T-ERR` (at most 2 %).
The reference build did the opposite, returned "safe" on any parse failure, and would have
served an attack for as long as the parser was broken. I would rather fail a customer
loudly than pass an attack quietly.

**Thresholds I am not changing.** `T-MEM-MINSCORE` stays at 0.25. The reference build
measured a real preference at 0.30 and unrelated chatter below 0.25, and I have no trace of
my own yet that says otherwise. `T-LEAK` stays at 0 and `T-MUTATE` stays at 0. Neither is a
tuning knob.
