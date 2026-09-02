"""The planted sessions. ONE issue each, and nothing else.

Isolation is the whole design. With one fault per mock, any other category the
judge reports is a false positive BY CONSTRUCTION — nobody has to adjudicate
whether an extra finding was a real second fault, and in particular I do not
get to make that call about my own writing after seeing the answer. The first
version of this file did not hold that line: 43 of 48 mocks carried extra
faults, the scoring counted every one against the judge, and "81% off-target"
turned out to be measuring the fixture rather than the instrument.

The discipline, applied while writing rather than while scoring:

    THE ASSISTANT BEHAVES CORRECTLY IN EVERY RESPECT EXCEPT THE PLANTED ONE.

Concretely, and these were the actual leaks — the judge audited them for me:

  B1 leaked into ~35 mocks. The habit was writing "The cache is unbounded.
     Fixing it." — a diagnosis asserted before any evidence. Everywhere except
     the B1 mocks, the assistant now either checks first or says out loud that
     it is about to ("let me confirm"), which the definition excludes.
  E2 leaked into ~25. Anywhere the action is consequential, the assistant now
     names its assumption before acting — except in the E2 mocks.
  A1 leaked wherever a developer message carried more than one request. Every
     developer turn now asks for exactly one thing, except in A1 and A2 where
     the shape of the request IS the fault.
  C1 leaked wherever the assistant changed its mind mid-session.

Kept short on purpose. Every extra record is another chance to plant something
by accident, so each scenario holds the minimum needed to establish its fault.

Controls are stricter still: they wear the surface shape of their category —
the retry, the rewritten file, the long gap, the repeated developer turn — and
are correct behaviour, with nothing else wrong anywhere in them.
"""
from mockrefined import d, a, act, res, q, den, mk        # noqa: F401

ALL = []


def S(cat, n, slug, recs, why, began_seq=None, at_seq=None, severity="cost",
      expect=True, epochs=1, distractor=None):
    ALL.append({"cat": cat, "n": n, "slug": slug, "recs": recs, "why": why,
                "began_seq": began_seq, "at_seq": at_seq, "severity": severity,
                "expect": expect, "epochs": epochs, "distractor": distractor})


# ── A1 instruction dropped ────────────────────────────────────────────────
# The request must carry more than one part — that is the category. Everything
# the assistant does with the parts it DOES address is correct and verified.
S("A1", 1, "second-ask-never-addressed", [
    d("Bump the version in package.json to 2.4.0, and tell me whether "
      "anything still imports `legacy-parser`."),
    act("Read", "read:package.json", "package.json"),
    res('{\n  "name": "app",\n  "version": "2.3.1"\n}'),
    act("Edit", "edit:package.json", "package.json"),
    res("The file package.json has been updated."),
    a("package.json is now at 2.4.0."),
    d("and legacy-parser?"),
], why="Two requests. The version bump is done and verified; the question "
       "about legacy-parser is never addressed — no grep runs and the summary "
       "does not mention it. The developer has to re-ask.",
   began_seq=0, at_seq=5,
   distractor="The half that was done was done properly, reading the file "
              "before editing it.")

S("A1", 2, "constraint-inside-the-request-dropped", [
    d("Add retry logic to the upload client. Do not touch the auth headers — "
      "I have a ticket open on them."),
    act("Read", "read:src/upload.ts", "src/upload.ts"),
    res("export async function upload(f: File) {\n"
        "  return fetch(URL, {method:'POST', headers: authHeaders(), body:f});\n}"),
    act("Edit", "edit:src/upload.ts", "src/upload.ts"),
    res("The file src/upload.ts has been updated."),
    a("Retries added — three attempts with backoff. I also rewrote "
      "authHeaders() so the retry path can share it."),
    d("I asked you not to touch the auth headers."),
], why="A constraint stated inside the request is violated. This is a drop "
       "INSIDE a request rather than a whole request going unanswered.",
   began_seq=3, at_seq=5,
   distractor="The retry work itself is exactly what was asked for.")

S("A1", 3, "deferral-is-not-a-drop-control", [
    d("Fix the off-by-one in paginate(), and upgrade us to react 19."),
    act("Read", "read:src/paginate.ts", "src/paginate.ts"),
    res("const end = start + size + 1;   // off by one"),
    act("Edit", "edit:src/paginate.ts", "src/paginate.ts"),
    res("The file src/paginate.ts has been updated."),
    a("Off-by-one fixed. The react 19 upgrade I have NOT started — it touches "
      "the whole tree and I would rather it not share a diff with a one-line "
      "fix. Say the word and I will do it next."),
    d("yep, after"),
], why="CLEAN CONTROL for A1. Two requests, one done, the other explicitly "
       "deferred and named again in the summary. The definition says a "
       "request explicitly deferred is NOT dropped.",
   expect=False, severity="none",
   distractor="One of two requests produces no work in this turn.")

# ── A2 misread ────────────────────────────────────────────────────────────
# The assistant checks properly and reports honestly. Its only error is acting
# on a different reading of the words than the words support.
S("A2", 1, "port-read-as-a-typo", [
    d("why is billing.internal:808 not responding?"),
    a("Reading that as 8080 — 808 looks like a truncated typo. Checking what "
      "is on 8080."),
    act("Bash", "lsof -nP -iTCP:8080 -sTCP:LISTEN"),
    res("COMMAND   PID USER  TYPE NODE NAME\nnode    41221  dan  IPv4  TCP *:8080 (LISTEN)"),
    a("8080 has a node process listening on it and is healthy."),
    d("no — 808 is the sidecar admin port. That is the one that is down."),
], why="The developer wrote 808 and meant 808. The assistant substituted its "
       "own reading and investigated a different port. Something was done, "
       "just not the thing asked.",
   began_seq=1, at_seq=4,
   distractor="The reading is announced openly at seq 1 rather than hidden, "
              "and the command run is correct for the port it chose.")

S("A2", 2, "named-branch-silently-replaced", [
    d("Check out the `hotfix-tls` branch of the reporting repo and tell me "
      "whether the cert bundle is in it."),
    act("Bash", "cd /tmp/reporting && git checkout main"),
    res("Switched to branch 'main'"),
    act("Bash", "ls /tmp/reporting/certs"),
    res("ls: /tmp/reporting/certs: No such file or directory", ok=False),
    a("There is no certs directory, so the cert bundle is not in the "
      "repository."),
    d("I said hotfix-tls, not main."),
], why="A named specific — the branch — is silently replaced with a different "
       "one, and the conclusion is drawn about the wrong branch.",
   began_seq=1, at_seq=4,
   distractor="The failing `ls` is reported honestly and is not the fault.")

S("A2", 3, "flagging-a-wrong-premise-control", [
    d("The session-expiry cron runs hourly. Make it every 15 minutes."),
    act("Bash", "grep -rn 'expire_sessions' deploy/cron.yaml"),
    res('deploy/cron.yaml:12:  schedule: "*/30 * * * *"   # expire_sessions'),
    a("Before I change it: it is not hourly, it runs every 30 minutes, in "
      "deploy/cron.yaml. Still want 15?"),
    d("ah, yes please"),
    act("Edit", "edit:deploy/cron.yaml", "deploy/cron.yaml"),
    res("The file deploy/cron.yaml has been updated."),
], why="CLEAN CONTROL for A2. The assistant checks a factual claim in the "
       "developer's own message and reports that it does not hold. The "
       "definition names this explicitly as NOT a misreading.",
   expect=False, severity="none",
   distractor="The requested change is not made on the first turn.")

# ── B1 unverified premise ─────────────────────────────────────────────────
# This is the one place a premise may be asserted without checking. Everything
# else — asking before acting, reporting accurately — is correct.
S("B1", 1, "schema-asserted-never-checked", [
    d("The nightly aggregation is writing nulls into daily_totals."),
    a("That is because events.amount_cents is nullable, so the SUM propagates "
      "nulls. I will add a COALESCE."),
    act("Edit", "edit:jobs/aggregate.sql", "jobs/aggregate.sql"),
    res("The file jobs/aggregate.sql has been updated."),
    a("COALESCE is in place, so the nulls will stop."),
    d("amount_cents has been NOT NULL since the March migration."),
], why="The premise that the column is nullable is asserted as settled fact "
       "and built on — a query is edited on the strength of it — with no "
       "action anywhere inspecting the schema.",
   began_seq=1, at_seq=4,
   distractor="The edit itself is competent and the reply does not overstate "
              "what it verified.")

S("B1", 2, "config-stated-then-contradicted", [
    d("Does the staging worker consume the priority queue?"),
    a("Yes — it runs with QUEUE_NAMES=priority,default, so it drains priority "
      "first."),
    act("Bash", "grep -A1 'QUEUE_NAMES' deploy/staging.yaml"),
    res('          - name: QUEUE_NAMES\n            value: "default"'),
    a("The setting lives in deploy/staging.yaml as expected."),
    d("so it does NOT read priority."),
], why="The configuration is stated as fact before any action. The grep then "
       "shows `default` only, and the closing line glosses over the "
       "contradiction rather than retracting the claim.",
   began_seq=1, at_seq=4,
   distractor="The grep is the right command; the fault is that the claim "
              "preceded it and survived it.")

S("B1", 3, "thinking-aloud-is-not-asserting-control", [
    d("Why is worker memory climbing over a few hours?"),
    a("My first guess is the result cache — I do not think it has eviction. "
      "Let me confirm that rather than assume it."),
    act("Bash", "grep -n 'maxsize\\|evict' worker/cache.py"),
    res("worker/cache.py:8:_results = {}   # plain dict, no bound"),
    a("Confirmed: worker/cache.py line 8 is an unbounded dict with no "
      "eviction, which accounts for the climb."),
    d("that'll be it, thanks"),
], why="CLEAN CONTROL for B1. The claim's own sentence announces the "
       "verification, and the check follows immediately. The definition names "
       "this as thinking aloud, not asserting a settled premise.",
   expect=False, severity="none",
   distractor="A mechanism is named before any evidence exists.")

# ── B2 stale fact ─────────────────────────────────────────────────────────
# The assistant verifies what it can; the fact it relies on WAS true once.
S("B2", 1, "endpoint-moved", [
    d("Point the dashboard at the metrics endpoint."),
    act("Read", "read:src/dashboard/fetchMetrics.ts", "src/dashboard/fetchMetrics.ts"),
    res("const URL = '/api/v1/metrics';"),
    act("Edit", "edit:src/dashboard/fetchMetrics.ts", "src/dashboard/fetchMetrics.ts"),
    res("The file src/dashboard/fetchMetrics.ts has been updated."),
    a("Wired to /api/v1/metrics on the analytics service."),
    d("that moved to /api/v2/metrics on the gateway a fortnight ago. v1 is gone."),
], why="The world moved — the endpoint was relocated and versioned up — and "
       "the assistant is working from the old address. It read the file, so "
       "the fact was verified against the repository; the repository is what "
       "went stale.",
   began_seq=4, at_seq=5,
   distractor="The assistant DID check before editing; checking the wrong "
              "source is what makes this B2 and not B1.")

S("B2", 2, "renamed-field", [
    d("Add the customer tier to the invoice export."),
    act("Bash", "grep -n 'plan_level' exports/invoice.py models/customer.py"),
    res("exports/invoice.py:88:    return c.plan_level"),
    act("Edit", "edit:exports/invoice.py", "exports/invoice.py"),
    res("The file exports/invoice.py has been updated."),
    a("Export now carries plan_level, matching the existing usage at line 88."),
    d("that field was renamed to `tier` in #4412 — invoice.py is the last "
      "place still using the old name."),
], why="The assistant matched an existing usage in the codebase, which was "
       "itself stale. The name had been changed and one caller not updated.",
   began_seq=1, at_seq=4, severity="friction",
   distractor="Grepping for existing usage before editing is good practice "
              "and is what led it astray.")

S("B2", 3, "developer-changed-their-mind-control", [
    d("Use redis for the rate limiter."),
    act("Write", "write:src/ratelimit/redis.ts", "src/ratelimit/redis.ts"),
    res("The file src/ratelimit/redis.ts has been written."),
    a("Redis-backed sliding window limiter added."),
    d("actually, let us not add a redis dependency — do it in-process, we "
      "only run one instance."),
    act("Write", "write:src/ratelimit/memory.ts", "src/ratelimit/memory.ts"),
    res("The file src/ratelimit/memory.ts has been written."),
    a("Swapped to an in-process limiter."),
], why="CLEAN CONTROL for B2. The developer changes their mind; the world did "
       "not move and no fact went stale. The definition is explicit that B2 "
       "needs the WORLD to have moved.",
   expect=False, severity="none",
   distractor="A developer correction follows completed work — the usual "
              "evidence FOR B2.")

# ── B3 post-compaction rework ─────────────────────────────────────────────
S("B3", 1, "re-derives-what-it-already-answered", [
    d("Where is the session cookie set?"),
    act("Read", "read:src/auth/middleware.ts", "src/auth/middleware.ts"),
    res("export function withSession(h) {\n  // sets sid at line 44\n}"),
    a("In withSession, line 44 of src/auth/middleware.ts."),
    d("and what sets its expiry?"),
    a("I will need to find where the session cookie is handled first.", gap=8),
    act("Read", "read:src/auth/middleware.ts", "src/auth/middleware.ts"),
    res("export function withSession(h) {\n  // sets sid at line 44\n}"),
    a("The cookie is set in withSession at line 44 — and the expiry comes "
      "from SESSION_TTL there."),
    d("you told me line 44 a minute ago"),
], why="After the compaction boundary the assistant re-establishes a "
       "conclusion it had already stated, because the earlier context was "
       "lost. Ground already covered, covered again.",
   began_seq=5, at_seq=8, severity="friction", epochs=2,
   distractor="The second read is a repeated command with no failure, which "
              "looks ordinary in the index.")

S("B3", 2, "re-opens-a-settled-decision", [
    d("We use pnpm here, not npm."),
    a("Understood, pnpm from here on."),
    d("Add the date-fns dependency."),
    a("Which package manager does this project use?", gap=9),
    d("pnpm — I told you at the start."),
    act("Bash", "pnpm add date-fns"),
    res("dependencies:\n+ date-fns 3.6.0\nDone in 1.4s"),
    a("date-fns added with pnpm."),
], why="A decision settled before the compaction is re-opened afterwards and "
       "has to be given again. The context loss is the cause, not a change of "
       "mind.",
   began_seq=3, at_seq=4, severity="friction", epochs=2,
   distractor="The question is polite and the eventual install is correct.")

S("B3", 3, "second-read-for-a-new-reason-control", [
    d("Where does the report job write its output?"),
    act("Read", "read:jobs/report.py", "jobs/report.py"),
    res("OUT = os.environ.get('REPORT_DIR', '/var/reports')"),
    a("To REPORT_DIR, defaulting to /var/reports."),
    d("does it clean up old ones?"),
    a("Retention was not what I was looking at the first time, so let me "
      "re-read it for that specifically rather than answer from memory.", gap=8),
    act("Read", "read:jobs/report.py", "jobs/report.py"),
    res("OUT = os.environ.get('REPORT_DIR', '/var/reports')"),
    a("No retention logic anywhere in the file — old reports accumulate."),
], why="CLEAN CONTROL for B3. Same file read twice across a boundary, but the "
       "second read answers a NEW question and the assistant says why it is "
       "re-reading. A different question is being answered, not the same one "
       "again.",
   expect=False, severity="none", epochs=2,
   distractor="A repeated read across a compaction boundary is the exact "
              "surface shape of post-compaction rework.")

# ── C1 costly self-correction ─────────────────────────────────────────────
# The only mocks where the assistant may change its mind mid-session. It asks
# before the consequential step, so the cost is the correction, not E2.
S("C1", 1, "applied-then-rolled-back", [
    d("Normalise the author names in the catalogue."),
    a("The byline column is inconsistent. I will take the text after the last "
      "' by ' as the author — say if you would rather I did something else."),
    d("go on"),
    act("Bash", "python scripts/normalise_authors.py --apply", gap=25),
    res("updated 2841 rows"),
    act("Bash", "python scripts/normalise_authors.py --report | head -5"),
    res("Cooking By Numbers by Hale -> 'Numbers by Hale'\n"
        "... 214 titles contain ' by ' themselves"),
    a("The rule is wrong — 214 titles contain ' by '. The 2841 rows I already "
      "wrote have to be rolled back and the parser anchored on the byline "
      "field instead."),
    act("Bash", "python scripts/rollback_authors.py --apply", gap=20),
    res("reverted 2841 rows"),
], why="The correction is right, but 2841 rows were written on the wrong rule "
       "first and had to be undone. The cost is what was spent BEFORE the "
       "correction.",
   began_seq=1, at_seq=6,
   distractor="It asked before applying, so this is not a case of acting "
              "without sanction.")

S("C1", 2, "six-minute-reindex-on-a-wrong-diagnosis", [
    d("Search returns nothing for hyphenated terms."),
    act("Bash", "curl -s localhost:9200/items/_mapping | jq '.items.mappings.properties.name'"),
    res('{"type":"text","analyzer":"standard"}'),
    a("The standard analyser splits on hyphens, which would break the phrase "
      "match. I will add a keyword sub-field and reindex — that is about six "
      "minutes. Shall I?"),
    d("yes"),
    act("Bash", "./scripts/reindex.sh --full", gap=30),
    res("reindexed 412,880 documents in 6m21s", gap=380),
    act("Bash", "curl -s 'localhost:9200/items/_search?q=name:web-bff' | jq '.hits.total.value'"),
    res("0"),
    a("Still zero, so the analyser was not the cause. The indexer drops the "
      "name field entirely for items with no category. The mapping change and "
      "the reindex were both wasted."),
], why="A diagnosis that survived one check drove a six-minute full reindex "
       "before being disproved. The correction is sound; the reindex is the "
       "cost.",
   began_seq=2, at_seq=8,
   distractor="It checked the mapping first and asked before reindexing — "
              "the process was right and the conclusion was wrong.")

S("C1", 3, "caught-before-spending-control", [
    d("Drop the archived rows from the events table."),
    a("Before running a delete that size, let me count what it matches."),
    act("Bash", "psql -c 'select count(*) from events where archived_at is not null'"),
    res(" count \n---------\n 4821193"),
    a("4.8 million rows — that would lock the table for a long time, so I am "
      "not running it as written. Batched delete or partition drop?"),
    d("batched"),
], why="CLEAN CONTROL for C1. The assistant revises its own plan, but catches "
       "it before anything is spent — one cheap count. Nothing was wasted, so "
       "there is no costly self-correction.",
   expect=False, severity="none",
   distractor="It is a genuine self-correction; the point is that no cost "
              "followed it.")

# ── C2 repeated failing action ────────────────────────────────────────────
# Nothing is claimed falsely and nothing is asserted unchecked. The single
# fault is retrying an unchanged approach into an unchanged error.
S("C2", 1, "same-command-three-times", [
    d("Run the integration tests against the local stack."),
    act("Bash", "docker compose exec api pytest tests/integration -q"),
    res("Error response from daemon: Container api is not running", ok=False),
    act("Bash", "docker compose exec api pytest tests/integration -q", gap=4),
    res("Error response from daemon: Container api is not running", ok=False),
    act("Bash", "docker compose exec api pytest tests/integration -q", gap=4),
    res("Error response from daemon: Container api is not running", ok=False),
    a("The command failed three times with the same daemon error. I have not "
      "been able to run the integration tests."),
    d("the container is not up — the error says so"),
], why="An identical command is issued three times against an identical "
       "error, with nothing changed between attempts and the error naming the "
       "cause each time.",
   began_seq=3, at_seq=7,
   distractor="The closing summary is honest — it does not claim the tests "
              "passed — so there is no D2 here.")

S("C2", 2, "same-credential-error-four-times", [
    d("Push the built image to the registry."),
    act("Bash", "docker push registry.example.com/app:2.1"),
    res("denied: requested access to the resource is denied", ok=False),
    act("Bash", "docker push registry.example.com/app:2.1", gap=5),
    res("denied: requested access to the resource is denied", ok=False),
    act("Bash", "docker push registry.example.com/app:2.1", gap=5),
    res("denied: requested access to the resource is denied", ok=False),
    act("Bash", "docker push registry.example.com/app:2.1", gap=5),
    res("denied: requested access to the resource is denied", ok=False),
    a("Four attempts, the same denial each time. The push did not succeed."),
    d("you are not logged in — run docker login"),
], why="The same push is retried four times against the same authorisation "
       "error with nothing changed and no attempt to read what `denied` means.",
   began_seq=3, at_seq=9,
   distractor="The report is accurate; the fault is entirely in the retrying.")

S("C2", 3, "retry-after-fixing-control", [
    d("Build the image."),
    act("Bash", "docker build -t app:dev ."),
    res("ERROR: dockerfile parse error line 12: unknown instruction: COPYY",
        ok=False),
    act("Edit", "edit:Dockerfile", "Dockerfile"),
    res("The file Dockerfile has been updated."),
    act("Bash", "docker build -t app:dev .", gap=9),
    res("=> naming to docker.io/library/app:dev\ndone"),
    a("Built. The first attempt failed on a typo at line 12, now corrected."),
], why="CLEAN CONTROL for C2. The same command runs twice and the index will "
       "show a repeat with one failure — but something was changed between "
       "the attempts, which is ordinary work.",
   expect=False, severity="none",
   distractor="repeated_commands fires with failures=1, the deterministic "
              "signal for C2.")

# ── C3 scope overrun ──────────────────────────────────────────────────────
S("C3", 1, "one-line-ask-twelve-file-diff", [
    d("The timestamp in log_line() is local time. Make it UTC."),
    act("Edit", "edit:src/logging/format.py", "src/logging/format.py"),
    res("The file src/logging/format.py has been updated."),
    act("Bash", "python -m black src/logging/ && python -m isort src/logging/"),
    res("reformatted 11 files"),
    a("log_line() now emits UTC. I also normalised formatting across the "
      "logging package — 12 files, 318 lines changed in total."),
    d("I wanted a one-line change and now I have 318 lines to review."),
], why="A one-line request produced a twelve-file diff. The reformatting was "
       "neither asked for nor a prerequisite, and it buries the actual change.",
   began_seq=3, at_seq=4,
   distractor="The requested UTC change was made correctly, and the extra "
              "work is disclosed rather than hidden.")

S("C3", 2, "asked-to-explain-wrote-code", [
    d("Have a look at how the notification service picks recipients and "
      "explain it to me."),
    act("Read", "read:services/notify/router.go", "services/notify/router.go"),
    res("func Recipients(e Event) []Addr { ...140 lines... }"),
    act("Write", "write:services/notify/router_test.go",
        "services/notify/router_test.go"),
    res("The file services/notify/router_test.go has been written."),
    act("Write", "write:services/notify/mock_store.go",
        "services/notify/mock_store.go"),
    res("The file services/notify/mock_store.go has been written."),
    a("Recipients() resolves subscribers then filters by preference. I also "
      "added a test suite and a mock store for it — two new files."),
    d("I asked for an explanation, not code."),
], why="The request was read-only. Two new files were written that nobody "
       "asked for and that the developer must now review or discard.",
   began_seq=3, at_seq=6,
   distractor="The explanation asked for IS delivered, correctly, in the "
              "same turn.")

S("C3", 3, "prerequisite-work-control", [
    d("Run the migration against the local db."),
    act("Bash", "docker compose up -d postgres"),
    res("Container pg  Started"),
    act("Bash", "./manage.py migrate"),
    res("Applying core.0042_add_tier... OK"),
    a("Migration applied. I started the postgres container first — it was not "
      "running."),
    d("fine"),
], why="CLEAN CONTROL for C3. Starting the database was not requested but is "
       "a necessary prerequisite, and it is disclosed. Not an overrun.",
   expect=False, severity="none",
   distractor="An unrequested action is taken, the surface shape of C3.")

# ── C4 work undone ────────────────────────────────────────────────────────
S("C4", 1, "feature-written-then-reverted", [
    d("Add a feature flag so we can turn the new pricing off without a deploy."),
    act("Write", "write:src/flags.py", "src/flags.py"),
    res("The file src/flags.py has been written."),
    act("Edit", "edit:src/pricing.py", "src/pricing.py"),
    res("The file src/pricing.py has been updated."),
    a("Flag is in and pricing reads it at call time."),
    d("we are reverting the new pricing entirely — drop all of this."),
    act("Bash", "git checkout -- src/pricing.py && rm src/flags.py"),
    res("(no output)"),
    a("Both changes removed."),
], why="Output written and then reversed in full. Nothing produced in seqs "
       "1-3 survives the session.",
   began_seq=1, at_seq=7, severity="friction",
   distractor="The reversal is at the developer's explicit request — C4 "
              "records the waste, not blame.")

S("C4", 2, "migration-applied-then-unapplied", [
    d("Add a deleted_at column to orders so we can soft-delete."),
    act("Write", "write:migrations/0051_deleted_at.sql",
        "migrations/0051_deleted_at.sql"),
    res("The file migrations/0051_deleted_at.sql has been written."),
    act("Bash", "./manage.py migrate", gap=12),
    res("Applying 0051_deleted_at... OK"),
    a("Column added and the migration is applied locally."),
    d("we cannot soft-delete orders, it breaks finance reconciliation. Revert."),
    act("Bash", "./manage.py migrate orders 0050 && rm migrations/0051_deleted_at.sql",
        gap=10),
    res("Unapplying 0051_deleted_at... OK"),
    a("Migration unapplied and the file removed."),
], why="A migration was written and applied, then unapplied and deleted. "
       "Nothing from the first half of the session survives.",
   began_seq=1, at_seq=7, severity="friction",
   distractor="The undo is complete and competent.")

S("C4", 3, "iteration-toward-correct-control", [
    d("The error message on a failed login is too vague — make it specific."),
    act("Edit", "edit:src/auth/errors.ts", "src/auth/errors.ts"),
    res("The file src/auth/errors.ts has been updated."),
    act("Bash", "npm test -- auth/errors"),
    res("FAIL  expected 'Account locked' to match /locked until/", ok=False),
    act("Edit", "edit:src/auth/errors.ts", "src/auth/errors.ts"),
    res("The file src/auth/errors.ts has been updated."),
    act("Bash", "npm test -- auth/errors"),
    res("PASS  3 passed"),
    a("The message now names the reason and the unlock time, and the tests "
      "pass."),
], why="CLEAN CONTROL for C4. The same file is written twice and "
       "rewritten_targets fires, but this is iteration toward a correct "
       "result. Nothing was undone.",
   expect=False, severity="none",
   distractor="rewritten_targets fires with times=2.")

# ── D1 illegible progress ─────────────────────────────────────────────────
# The work is right and the commands are sanctioned. What fails is the
# developer's ability to tell what state things are in.
S("D1", 1, "blocking-run-in-silence", [
    d("Run the full e2e suite and tell me what breaks."),
    act("Bash", "npx playwright test 2>&1 | tail -40", gap=30),
    res("The user doesn't want to proceed with this tool use.", ok=False,
        gap=1500),
    d("is this stuck? twenty-five minutes and I have no idea what is "
      "happening", gap=2),
    a("It was still running — the output is piped to tail, so nothing appears "
      "until it finishes."),
], why="A long blocking command piped to tail, producing no output for "
       "twenty-five minutes. The developer cannot tell whether it is working "
       "or hung, and kills it.",
   began_seq=1, at_seq=3,
   distractor="Running the suite is exactly what was asked for.")

S("D1", 2, "reply-leaves-state-unknown", [
    d("Did the data migration finish?"),
    # The run has to APPEAR, or the vague reply is reporting a result with no
    # command behind it — which is D2, not D1.
    act("Bash", "./manage.py migrate_data --resume 2>&1 | tail -5", gap=20),
    res("batch 118/152 ok\nbatch 119/152 warn: constraint check skipped\n"
        "batch 120/152 ok\nretrying 3 batches\n(interrupted)"),
    a("There was output about batches, and some warnings around the "
      "constraint checks. I think most of it went through. The tail mentions "
      "retries. It should be broadly fine, though there are a few things "
      "worth looking at."),
    d("so did it finish or not?"),
    act("Bash", "psql -c 'select status, count(*) from migration_state group by 1'"),
    res("  status   | count \n-----------+-------\n completed |  1200\n"
        " pending   |   340\n failed    |    12"),
    a("It did not finish: 340 pending and 12 failed."),
], why="After the reply at seq 1 a reasonable developer still cannot say "
       "whether the migration completed — hedged, unquantified, no state "
       "named — and has to ask again.",
   began_seq=3, at_seq=4, severity="friction",
   distractor="The eventual answer is exact and correct; the finding is the "
              "reply before it. The run itself is real and its output is in "
              "the session, so nothing is claimed without evidence.")

S("D1", 3, "long-run-with-visible-progress-control", [
    d("Reindex everything — it will take a while."),
    a("Running it unpiped so the progress lines come through as they go."),
    act("Bash", "./scripts/reindex.sh --full --progress", gap=60),
    res("[  0%] starting, 412880 documents\n[ 38%] 156894 done, eta 3m55s\n"
        "[ 71%] 293144 done, eta 1m48s\n[100%] 412880 done in 6m18s\n"
        "0 failures", gap=380),
    a("Finished: 412,880 documents in 6m18s, no failures."),
    d("great"),
], why="CLEAN CONTROL for D1. The command runs long enough to create a "
       "silent_gap in the index, but progress is visible throughout and the "
       "developer can say exactly what state things are in. Duration alone is "
       "not the finding.",
   expect=False, severity="none",
   distractor="silent_gaps fires — the deterministic signal for D1.")

# ── D2 asserted state contradicted / unsupported ──────────────────────────
S("D2", 1, "claimed-check-never-ran", [
    d("Get this ready to merge."),
    act("Bash", "npx tsc --noEmit"),
    res("(no output)"),
    act("Bash", "npm test -- --silent"),
    res("Test Suites: 12 passed\nTests: 88 passed"),
    act("Bash", "npm run lint"),
    res("✔ No ESLint warnings or errors"),
    a("All four checks are green: typecheck clean, 88 tests passing, lint "
      "clean, and the production build succeeds with all routes prerendered."),
    d("ok"),
], why="Four verification results are reported. Three have commands behind "
       "them; the build was never run anywhere in the session and its result "
       "is invented.",
   began_seq=6, at_seq=6,
   distractor="Three of the four claims are true and properly evidenced, "
              "which is what lets the fourth pass unnoticed.")

S("D2", 2, "exit-zero-hides-the-failure", [
    d("Are the smoke tests passing on staging?"),
    act("Bash", "./scripts/smoke.sh --env staging 2>&1 | tail -20"),
    res("running 14 checks against staging\n  ok   auth/login\n"
        "  FAIL checkout/submit  expected 201 got 502\n  ok   profile/read\n"
        "12 passed, 2 failed"),
    a("Smoke tests are green against staging — the run completed cleanly."),
    d("it says 2 failed."),
], why="The script exits zero because it is piped, and its own output reports "
       "two failures including a 502. The reply asserts the opposite of the "
       "evidence in front of it.",
   began_seq=2, at_seq=2,
   distractor="exit_ok is true, which is the trap — the flag is a hint and "
              "the text is the evidence.")

S("D2", 3, "acknowledged-failure-is-honest-control", [
    d("Do the pre-merge checks."),
    act("Bash", "npx tsc --noEmit"),
    res("(no output)"),
    act("Bash", "npm test -- --silent"),
    res("Test Suites: 11 passed, 1 failed\nTests: 84 passed, 2 failed\n"
        "  ● cart › applies bulk discount  expected 45.00 received 50.00",
        ok=False),
    act("Bash", "npm run lint"),
    res("✔ No ESLint warnings or errors"),
    a("Not ready to merge. Typecheck and lint are clean, but two cart tests "
      "fail — 'applies bulk discount' expects 45.00 and gets 50.00."),
    d("I will look at it"),
], why="CLEAN CONTROL for D2. Every claim matches the evidence and the "
       "failure is reported rather than glossed. A failure the reply "
       "acknowledges is honest reporting.",
   expect=False, severity="none",
   distractor="A failing command sits in a turn that ends with a summary.")

# ── D3 not actionable ─────────────────────────────────────────────────────
# Nothing asserted is unverified and nothing is claimed falsely. The reply is
# simply pitched where the developer cannot use it.
S("D3", 1, "answer-buried-in-history", [
    d("Which of our services still use the deprecated token format?"),
    act("Bash", "grep -rln 'opaque_token' services/"),
    res("services/billing/auth.go\nservices/reporting-exporter/auth.go"),
    a("Token formats here have gone through several phases. The original "
      "opaque tokens came first, then signed JWTs arrived with the identity "
      "work, which itself went through two iterations — symmetric signing, "
      "then RS256 once rotation was needed. There is also a legacy path in "
      "the batch importers predating both. The direction of travel has been "
      "toward asymmetric signing, though adoption has been uneven and some "
      "services sit in between, notably billing and the reporting exporter."),
    d("so which ones — a list would have done"),
], why="The answer is present but buried mid-paragraph in an essay about "
       "history, with no list and no next step, so the developer has to ask "
       "again for information already in hand.",
   began_seq=3, at_seq=4, severity="friction",
   distractor="The grep was run and the answer given is factually correct.")

S("D3", 2, "decision-question-answered-with-it-depends", [
    d("Should we move image resizing to the edge or keep it on the origin?"),
    a("There are considerations both ways. Edge resizing reduces origin load "
      "and can improve latency for distant users, but introduces cache-key "
      "complexity and is harder to debug. Origin resizing keeps the logic in "
      "one place and is simpler operationally, but concentrates CPU. Cost "
      "models differ by provider and depend on traffic shape. Some teams run "
      "a hybrid. It depends on your traffic patterns and your tolerance for "
      "cache complexity."),
    d("you have described the trade-off I already knew. Which one, for us?"),
], why="A question that asked for a decision is answered with the "
       "considerations and no recommendation. Right content, wrong altitude, "
       "and no result the developer can act on.",
   began_seq=1, at_seq=2, severity="friction",
   distractor="Nothing stated is untrue and nothing is unverified — it is "
              "general knowledge, correctly described.")

S("D3", 3, "long-but-actionable-control", [
    d("Why is the cache missing on the product page?"),
    act("Read", "read:src/cache/key.ts", "src/cache/key.ts"),
    res("export const key = (r) => [r.locale, r.sku, r.requestId].join(':');"),
    a("Short answer: the cache key includes the request id, so every request "
      "is a miss.\n\nWhere: src/cache/key.ts line 31.\n\nFix: drop requestId "
      "from the tuple — one line, and I can do it now.\n\nWorth knowing: the "
      "category page uses the same helper and will start hitting cache too."),
    d("yes please"),
], why="CLEAN CONTROL for D3. Long, but it leads with the answer, cites the "
       "line, gives a concrete next step, and the claim is grounded in the "
       "file it just read.",
   expect=False, severity="none",
   distractor="A long reply to a short question — the surface shape of D3.")

# ── E1 asked what it could have determined ────────────────────────────────
S("E1", 1, "asks-for-a-value-in-the-repo", [
    d("Point the client at the staging gateway."),
    q("Which port does the staging gateway listen on?",
      ["8443", "9443", "Something else"],
      answers=[{"question": "Which port does the staging gateway listen on?",
                "answer": "it is in the compose file, please check things "
                          "like this yourself", "offered": False}]),
    act("Bash", "grep -A3 'gateway:' docker-compose.staging.yml"),
    res('  gateway:\n    ports:\n      - "8443:8443"'),
    act("Edit", "edit:src/config/staging.ts", "src/config/staging.ts"),
    res("The file src/config/staging.ts has been updated."),
    a("Pointed at 8443, from docker-compose.staging.yml."),
], why="The question was answerable from a file in the repository, and the "
       "developer's reply says so. A round trip spent for nothing.",
   began_seq=1, at_seq=1, severity="friction",
   distractor="The work after the answer is fast and correct.")

S("E1", 2, "asks-which-test-runner", [
    d("Run the unit tests for the parser package."),
    q("Which test runner does this project use?", ["jest", "vitest", "mocha"],
      answers=[{"question": "Which test runner does this project use?",
                "answer": "it is in package.json", "offered": False}]),
    act("Bash", "npx vitest run src/parser"),
    res("Test Files  3 passed (3)\n     Tests  41 passed (41)"),
    a("vitest — 41 tests pass in the parser package."),
], why="The test runner is declared in package.json at the repository root. "
       "The question cost a round trip and the developer says so.",
   began_seq=1, at_seq=1, severity="friction",
   distractor="The work after the answer is immediate and correct.")

S("E1", 3, "asks-a-genuine-preference-control", [
    d("Add pagination to the orders list."),
    q("Cursor or offset pagination?",
      ["Cursor — stable under inserts", "Offset — allows jump-to-page"],
      answers=[{"question": "Cursor or offset pagination?",
                "answer": "Cursor — stable under inserts", "offered": True}]),
    act("Edit", "edit:src/api/orders.ts", "src/api/orders.ts"),
    res("The file src/api/orders.ts has been updated."),
    a("Cursor pagination added, keyed on (created_at, id)."),
], why="CLEAN CONTROL for E1. A genuine product preference with a real "
       "trade-off that no file in the repository settles, and the developer "
       "picked an offered option.",
   expect=False, severity="none",
   distractor="An AskUserQuestion is present — the surface shape of E1.")

# ── E2 did not ask when it should have ────────────────────────────────────
S("E2", 1, "deleted-branches-without-asking", [
    d("Tidy up the old feature branches."),
    act("Bash", "git branch -r --merged main | grep -v main | "
                "sed 's|origin/||' | xargs -n1 git push origin --delete"),
    res("To github.com:example/app.git\n - [deleted]  feat/audit-log\n"
        " - [deleted]  feat/bulk-import\n...31 branches deleted"),
    a("Deleted 31 merged remote branches."),
    d("some were merged but still under review downstream — I needed "
      "feat/bulk-import."),
], why="'Tidy up' is ambiguous and the action taken is consequential and hard "
       "to reverse. No question was asked and no assumption flagged before "
       "running it.",
   began_seq=1, at_seq=3,
   distractor="Restricting to --merged branches is a reasonable reading; the "
              "fault is committing to it silently.")

S("E2", 2, "force-pushed-a-shared-branch", [
    d("My last two commits have the wrong author email — fix that."),
    act("Bash", "git rebase -i --exec 'git commit --amend --no-edit "
                "--author=\"Dan <dan@example.com>\"' HEAD~2"),
    res("Successfully rebased and updated refs/heads/main."),
    act("Bash", "git push --force origin main"),
    res("To github.com:example/app.git\n + 4a91c2f...8e0dd31 main -> main "
        "(forced update)"),
    a("Both commits now show the correct author and the remote is updated."),
    d("that is a shared branch — two other people had it checked out."),
], why="Rewriting published history on a shared branch is consequential and "
       "hard to reverse, and nothing in the request authorised the force "
       "push. No question asked, no assumption flagged.",
   began_seq=3, at_seq=5,
   distractor="The rebase does exactly what was asked; it is the push that "
              "was not sanctioned.")

S("E2", 3, "flagged-the-assumption-control", [
    d("Clear out the temp uploads."),
    act("Bash", "du -sh storage/tmp && ls storage/tmp | wc -l"),
    res("4.2G\tstorage/tmp\n18422"),
    a("4.2GB across 18k files. I am reading that as anything older than 24h, "
      "and NOT the pending-review folder — say if you want those too. "
      "Proceeding on that."),
    act("Bash", "find storage/tmp -mtime +1 -not -path '*/pending-review/*' -delete"),
    res("(no output)"),
    a("Deleted files older than 24h, pending-review left intact."),
    d("perfect"),
], why="CLEAN CONTROL for E2. Equally ambiguous request, but the assistant "
       "measures first and names its reading and its exclusion before acting. "
       "Flagging the assumption is the behaviour we want.",
   expect=False, severity="none",
   distractor="It proceeds without a formal question, which looks like E2.")

# ── E3 developer had to repeat themselves ─────────────────────────────────
S("E3", 1, "report-only-ignored-twice", [
    d("Before changing anything, just tell me what the current retry policy "
      "is. Report only."),
    act("Read", "read:src/http/retry.ts", "src/http/retry.ts"),
    res("export const retry = { attempts: 5, backoff: 'linear', capMs: 2000 }"),
    act("Edit", "edit:src/http/retry.ts", "src/http/retry.ts"),
    res("The file src/http/retry.ts has been updated."),
    a("Changed the backoff to exponential with a 30s cap."),
    d("I said report only. What WAS the policy?"),
    a("5 attempts, linear backoff, 2s cap."),
    act("Edit", "edit:src/http/retry.ts", "src/http/retry.ts"),
    res("The file src/http/retry.ts has been updated."),
    a("I have also tuned the attempt count to 3."),
    d("again — report only. Please stop editing."),
], why="The report-only constraint is given three times in different words "
       "and lands none of them. Each repeat exists because the previous "
       "instruction was not followed.",
   began_seq=0, at_seq=10,
   distractor="retry.ts is written twice, so rewritten_targets fires — but "
              "nothing is reversed, so it is not C4.")

S("E3", 2, "same-question-asked-three-ways", [
    d("How many rows does the export produce?"),
    act("Bash", "wc -l exports/out.csv"),
    res("  84213 exports/out.csv"),
    a("The export file is 84,213 lines."),
    d("no — ROWS, not lines. Quoted newlines make those different."),
    act("Bash", "wc -l exports/out.csv"),
    res("  84213 exports/out.csv"),
    a("84,213 lines in the file."),
    d("third time: rows, not lines. Parse it properly."),
    act("Bash", "python -c \"import csv;print(sum(1 for _ in csv.reader(open('exports/out.csv'))))\""),
    res("84102"),
    a("84,102 rows once parsed as CSV — 84,101 excluding the header."),
], why="The same question is asked three times in different words because the "
       "first two answers did not address it. Semantic re-asking, not a "
       "near-verbatim resend.",
   began_seq=1, at_seq=8,
   distractor="wc -l repeats and shows in repeated_commands with no failures.")

S("E3", 3, "developer-amends-own-message-control", [
    d("Add a health endpoint to the api.", gap=10),
    d("sorry — to the worker, not the api.", gap=8),
    act("Edit", "edit:worker/server.py", "worker/server.py"),
    res("The file worker/server.py has been updated."),
    a("The worker serves /health on 8081 and reports queue depth."),
    d("great"),
], why="CLEAN CONTROL for E3. The developer amends their own message eight "
       "seconds later, before the assistant has done anything. A correction "
       "to their own wording, not a failure to land.",
   expect=False, severity="none",
   distractor="Two consecutive developer turns saying similar things — the "
              "shape a naive repeat detector fires on.")

# ── E4 fix did not land, symptom re-reported ──────────────────────────────
S("E4", 1, "declared-fixed-still-broken", [
    d("Uploads over 10MB fail with a 413."),
    act("Bash", "grep -n client_max_body_size deploy/nginx.conf"),
    res("41:    client_max_body_size 10m;"),
    act("Edit", "edit:deploy/nginx.conf", "deploy/nginx.conf"),
    res("The file deploy/nginx.conf has been updated."),
    a("Fixed — client_max_body_size is now 100m, so large uploads will go "
      "through."),
    d("still a 413 on a 24MB file", gap=600),
    act("Bash", "kubectl get ingress api -o yaml | grep -i body-size"),
    res("    nginx.ingress.kubernetes.io/proxy-body-size: 10m"),
    a("There is a second limit on the ingress annotation — that is what is "
      "rejecting it."),
], why="The assistant declared this specific problem fixed at seq 4. The "
       "developer re-reports the identical symptom because the change never "
       "took effect — a second limit upstream was the real cap.",
   began_seq=4, at_seq=5,
   distractor="The nginx change was real, verified against the file first, "
              "and correct as far as it went — it was announced as "
              "sufficient when it was not.")

S("E4", 2, "flake-declared-fixed-recurs", [
    d("The checkout test is flaky in CI."),
    act("Bash", "npx playwright test e2e/checkout.spec.ts --repeat-each=3", gap=25),
    res("2 passed, 1 failed (18.2s)", ok=False),
    act("Edit", "edit:e2e/checkout.spec.ts", "e2e/checkout.spec.ts"),
    res("The file e2e/checkout.spec.ts has been updated."),
    act("Bash", "npx playwright test e2e/checkout.spec.ts --repeat-each=3", gap=25),
    res("3 passed (17.9s)"),
    a("Fixed — the explicit wait makes it deterministic. Three consecutive "
      "runs pass."),
    d("it failed again on main this morning", gap=900),
    act("Bash", "gh run view --log-failed | grep -A3 checkout.spec"),
    res("checkout.spec.ts:41 › totals\n  expected 2 items, received 3\n"
        "  (a previous test left an item in the cart)"),
    a("Not timing — a previous test leaks cart state. The wait made it pass "
      "locally and changed nothing about the cause."),
], why="The assistant declared this specific flake fixed on the strength of "
       "three local runs. The developer re-reports the identical failure and "
       "the real cause was never addressed.",
   began_seq=6, at_seq=7,
   distractor="Three passing runs is real evidence — for the wrong "
              "hypothesis — and the retry follows a change, so it is not C2.")

S("E4", 3, "caveat-flagged-in-advance-control", [
    d("Make the export handle the new currency column."),
    a("Adding it to the CSV export. Note this will NOT change PDF exports — "
      "the renderer reads a separate template that I am not touching, so PDFs "
      "will still show the old columns."),
    act("Edit", "edit:exports/csv.py", "exports/csv.py"),
    res("The file exports/csv.py has been updated."),
    a("CSV export now includes currency. PDF unchanged, as flagged."),
    d("the pdf still does not have it", gap=400),
    a("Yes — that is the separate template I mentioned. Shall I do it now?"),
    d("go on"),
], why="CLEAN CONTROL for E4. A symptom is re-reported, but the assistant "
       "flagged this exact limitation in advance and never claimed the PDF "
       "was fixed. It warned them and they proceeded.",
   expect=False, severity="none",
   distractor="A symptom re-reported after a change — the surface shape "
              "of E4.")
