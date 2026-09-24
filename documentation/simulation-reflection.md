## Summary of Implementation Changes:

Commit `0fb2291c6393ce4cdd295aafc75bf8eb39109c70` added our live trading client for **Planet 7 (P07)** alongside the practice-server files. Our main contribution was `live_client.py`: a bot that reads the current game state, estimates which supplies will run out first, and trades automatically.

We separated connection handling, resource calculations, and `SurvivalBot` decision logic. The server owns the official inventory; our bot chooses requests. Each state update follows **observe → check limits → accept → offer → advertise**, stopping after one action. This keeps the flow easy to explain.

Our survival estimate subtracts recent production from consumption, then calculates how many ticks each supply should last. We protect roughly three ticks of net demand and prefer paying with our own specialty. This helps us focus on shortages rather than simply accumulating resources.

Our first clue about a planet's specialty was its initial advertisement: it probably sells what it has in abundance. The bot updates that guess using later advertisements, offers, and trades, giving completed trades more weight without counting repeated records twice. Other planets' specialties remain private.

**Function guide** — functions and methods in `live_client.py`:

| Function | Purpose |
|---|---|
| `bundle_values()` | Converts resource quantities into an easy-to-use dictionary. |
| `effective_upkeep()` | Estimates net resource consumption after production. |
| `survival_score()` | Estimates the time until the first supply runs out. |
| `print_state()` | Displays inventory, health, market activity, and limits. |
| `print_result()` | Displays whether a command succeeded. |
| `print_protocol_error()` | Displays communication errors and whether to disconnect. |
| `SurvivalBot.__init__()` | Sets up market memory and duplicate tracking. |
| `new_request_id()` | Creates a unique identifier for each new command. |
| `add_evidence()` | Adds a clue about another planet's likely specialty. |
| `observe_market()` | Collects new clues from advertisements, offers, and trades. |
| `specialty_estimate()` | Returns the leading specialty guess and its evidence-based confidence. |
| `specialty_evidence_score()` | Retrieves a score used to rank trading partners. |
| `print_market_estimates()` | Displays specialty guesses or reports insufficient evidence. |
| `command_space_available()` | Checks room for another stored command result. |
| `command_allowed_this_tick()` | Checks the current tick's command allowance. |
| `accept_helpful_offer()` | Selects a small gift or a safe trade that improves survival. |
| `choose_resources()` | Chooses the most urgent non-specialty resource and a safe payment. |
| `remaining_ticks()` | Helper inside `choose_resources()` that estimates a supply's duration. |
| `make_helpful_offer()` | Proposes a one-for-one exchange matching another planet's advertisement. |
| `advertise_needs()` | Publishes our needs without replacing an already-matching advertisement. |
| `act()` | Checks whether trading is allowed and follows our action priorities. |
| `main()` | Connects, confirms P07's identity and readiness, and processes server messages. |

**Other additions:** `bazaar.proto` defines messages; generated `bazaar_pb2.py` translates them for Python. The client README, two Linux server executables, and validation credentials/report support the supplied practice exercise. They are separate from our strategy. `.DS_Store` is unrelated macOS metadata.

The client uses `BAZAAR_TOKEN`, validates outgoing trading messages, and prints decisions and errors. The practice report records a completed scripted exchange. Automated tests and persistent decision logging are still missing.

## Results of Simulation

Across our three classroom simulations, our first attempt failed quickly. After adding survival calculations and specialty estimates, P07 appeared much more competitive in the second and third runs, and we remember it being among the later planets still alive. We noticed that planets trading actively early on often lasted longer. Toward the end, some seemed to hold onto supplies while others struggled.

During the second simulation, we felt our own planet was becoming too greedy and had built up one of the larger survival buffers. We disconnected to revise the approach because we wanted the ecosystem to survive too. By “more ticks,” we mean supplies lasting more future game steps.

The committed client reflects that intent through small exchanges: outgoing offers give one unit for one unit, incoming trades allow only 1–3 total units per side, and gifts above three units are skipped. It also waits while an outgoing offer is open and avoids repeating the same proposal to the same partner within a tick. These rules limit individual exchanges, but they do not guarantee cooperation: ordinary incoming trades still need to improve P07's survival, and the bot neither sends gifts nor measures other planets' needs. The commit contains the final client, not separate versions of each classroom revision.

The supplied [run-2-log.json](run-2-log.json), internally identified as `run-40`, gives a more precise picture of one run:

- P07, named **Cinder**, completed **67 transactions**, importing 41 water and 41 components while exporting 57 food.
- P07 failed at **tick 81 of 120**, with **229 food but zero water and components**. Four planets outlasted it, so this log qualifies our recollection of being among the last few survivors.
- Only **two of nine planets** survived the full run. A planet that completed no trades failed at tick 40; the two survivors completed 93 and 259 transactions. Trading mattered, although transaction count alone does not explain survival.

**Personal reflection**

We were initially focused on keeping Planet 7 alive, so seeing it last longer after our revisions felt like progress. During the second simulation, though, we started questioning what we were optimizing for. If our planet built up a comfortable buffer while its trading partners ran out of supplies, that success would be difficult to sustain. Disconnecting to rethink our approach came from wanting our bot to contribute to a functioning market.

The log makes that lesson concrete: we could have plenty of food and still fail because we lacked water and components. Smaller trades were a useful adjustment, but they were only a first step toward cooperation. Next time, we would save each decision with the inventory and reasoning behind it, and test situations where two resources are equally scarce. We want to be able to explain why the bot acted, where its assumptions failed, and how a change could help the wider system survive.
