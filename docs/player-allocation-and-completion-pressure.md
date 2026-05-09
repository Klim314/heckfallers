# Player Allocation and Completion-Based Pressure

This note captures the next simulation model for player behavior and scalable
war contribution. It is intended as follow-up design work for the current hex
front POC.

The key correction from the initial POC model is that deployed players do not
directly generate liberation pressure. In a Helldivers-style war, players only
advance the front when they complete successful operations. A failed or pyrrhic
enemy victory contributes zero liberation progress.

## Goals

- Simulate player behavior without ticking every player individually.
- Preserve randomness while scaling to large active populations.
- Separate player presence from actual war contribution.
- Distinguish objective enemy response from player-perceived difficulty.
- Leave a clean path for replacing fake behavior with real telemetry.

## Core Terms

### Actual Threat Score

`actual_threat_score` is the objective enemy response level for a cell. It is a
system value used by the simulation and enemy AI.

It can affect:

- enemy reinforcement rate
- patrol density
- enemy resistance on contested cells
- mission modifier severity
- fortress and resistance-node activity
- success probability for operations
- completion time for operations

Example:

```text
actual_threat_score =
  base_enemy_strength
+ fortress_influence
+ resistance_node_influence
+ recent_deployment_response
+ enemy_supply_support
+ counteroffensive_bonus
- se_artillery_suppression
- fob_logistics_disruption
```

### Perceived Difficulty

`perceived_difficulty` is the player-facing or behavior-model estimate of how
hard, annoying, risky, or inefficient a cell feels.

It should not be identical to `actual_threat_score`. Players react to visible
signals, past experience, community reputation, biome friction, modifiers, and
recent failures.

Example:

```text
perceived_difficulty =
  visible_actual_threat
+ biome_difficulty_bias
+ mission_type_failure_bias
+ modifier_reputation
+ recent_failure_rate_signal
+ community_reputation
- visible_friendly_support
- recent_success_momentum
```

Enemy AI should use `actual_threat_score`. Player allocation should use
`perceived_difficulty`.

### Deployment

A deployment is a group of players choosing to run operations for a cell.
Deployment does not move the front by itself.

Deployments affect:

- social gravity
- enemy response
- expected future completions
- perceived momentum
- matchmaking attractiveness

### Completion

A completion is the resolution of a deployment after a mission or operation
duration. Only successful completions create liberation pressure.

```text
successful_completions -> liberation pressure
failed_completions     -> zero liberation pressure
```

Failures may still affect telemetry, perceived difficulty, and enemy confidence,
but they should not grant partial progress.

## Scalable Simulation Shape

At large scale, simulate flows of players rather than individual players.

```text
available players
  -> allocation model
  -> mission buckets
  -> completion resolution
  -> successful completion pressure
  -> front progress
```

For 150,000 active players with a 15 minute average mission cycle:

```text
150,000 / 900s ~= 167 players completing per second
```

The allocator only needs to process players who become available, not all active
players every tick.

## Player Cohorts

Use cohorts to get believable macro behavior without per-player simulation.

Initial cohorts:

| Cohort | Behavior |
| --- | --- |
| Casuals | Prefer safe, obvious, high-success cells. Strongly avoid perceived difficulty. |
| Order followers | Strongly follow Major Orders or command pings. Difficulty is secondary. |
| Optimizers | Prefer reward per minute and high expected success efficiency. |
| Veterans | Tolerate or seek high difficulty up to a point. Avoid hopeless cells. |
| Social followers | Prefer cells with friends, high population, or visible momentum. |
| Novelty seekers | Prefer new biomes, objectives, events, and underplayed fronts. |

Each cohort owns weights for the utility function, completion duration, and
success probability sensitivity.

## Cell Utility

Each allocation pass computes a utility score for every valid deployment cell.

```text
utility(cell, cohort) =
  strategic_value_weight      * perceived_strategic_value
+ reward_weight              * expected_reward_value
+ urgency_weight             * urgency
+ social_weight              * social_gravity
+ novelty_weight             * novelty
+ role_fit_weight            * role_fit
+ momentum_weight            * momentum
- difficulty_weight          * perceived_difficulty
- repetition_weight          * repetition_fatigue
- attention_friction_weight  * attention_friction
- switching_cost_weight      * switching_cost
- congestion_weight          * congestion_penalty
```

Some cohorts may invert or reshape terms. For veterans, difficulty can be
positive until a preferred challenge level, then negative:

```text
veteran_difficulty_appeal =
  min(perceived_difficulty, preferred_challenge_level)
- overload_penalty
```

### Strategic Value

How much the cell appears to matter.

Inputs:

- route to capital
- chokepoint score
- adjacent enemy count
- adjacent friendly exposure
- fortress or resistance-node influence
- FOB or artillery protection value
- isolation or encirclement potential
- supply-line importance, if added later

For real players, this must be visible. A strategically important cell that is
not communicated well has low perceived strategic value.

### Reward Value

Expected value adjusted by success chance and time.

```text
expected_reward_value =
  reward_amount
* estimated_success_probability
/ estimated_completion_time
```

Reward can move players into high-threat areas, but only when the reward feels
achievable.

### Urgency

Time-boxed reasons to move.

Examples:

- enemy offensive in progress
- FOB under attack
- fortress shield temporarily exposed
- final capture push
- defense timer close to failure

Urgency should decay or expire. Permanent urgency becomes background noise.

### Social Gravity

Players tend to follow population, friends, and visible community focus.

```text
social_gravity =
  log(active_deployments_on_cell + nearby_deployments + 1)
+ friend_or_squad_bonus
+ command_marker_bonus
```

This should be balanced by congestion so one cell does not absorb everyone.

### Novelty

Soft pressure away from repeating the same thing.

Inputs:

- biome not recently played
- objective not recently played
- enemy mix not recently played
- rare event active
- underplayed front

### Attention Friction

This replaces the earlier idea of travel friction. Physical travel is near zero
because drops can happen anywhere valid, but players still have interface and
attention cost.

Inputs:

- off-screen or requires panning
- visually unremarkable
- lacks a timer, icon, or callout
- strategic meaning requires inspection
- buried in a very large front

For the current small POC map, this can be zero.

### Switching Cost

Players do not instantly move fronts just because utility changed.

Inputs:

- current squad already committed
- current operation chain not finished
- friends already deployed elsewhere
- player just chose a loadout or difficulty
- desire to run one more mission on the same front

Switching cost creates allocation inertia.

### Congestion Penalty

Prevents every cohort from piling into the same cell.

```text
congestion_penalty =
  max(0, active_deployments_on_cell - useful_capacity)
```

This should be soft. Players can still overcommit, but the expected utility
falls.

## Allocation

Run allocation at a coarse cadence, such as once per second. The war front can
still tick at a higher rate.

For each cohort:

1. Determine available players.
2. Compute utility for valid contested cells.
3. Convert utilities to probabilities.
4. Sample allocations.
5. Create mission buckets.

Probability:

```text
p(cell) = softmax(utility(cell) / temperature)
```

Temperature controls concentration:

```text
low temperature  -> players converge on the highest-utility targets
high temperature -> players spread more evenly
```

Allocation:

```text
allocations = multinomial(available_players, p)
```

For small counts, use stochastic rounding when needed:

```text
expected = 3.4
allocate 3, with a 40% chance to allocate 1 more
```

This introduces randomness without simulating each player.

## Mission Buckets

Mission buckets are aggregate in-flight operations.

```python
@dataclass
class MissionBucket:
    cell_id: str
    cohort_id: str
    player_count: int
    started_at_s: float
    resolves_at_s: float
    success_probability: float
    contribution_per_success: float
    expected_completion_time_s: float
```

The implementation can store buckets directly or use a timing wheel:

```text
completions_by_second[resolve_second][cell_id][cohort_id] += bucket
```

The timing wheel is better for scale because the simulation only inspects due
completions.

## Completion Resolution

At each allocation or completion cadence:

```text
for bucket in due_buckets:
    successes = binomial(bucket.player_count, bucket.success_probability)
    failures = bucket.player_count - successes

    pressure_pulse = successes * bucket.contribution_per_success
    cell.pending_liberation += pressure_pulse

    available_players[bucket.cohort_id] += bucket.player_count

    record telemetry:
      successes
      failures
      completion_time
      actual_threat_score
      perceived_difficulty
      biome
      mission_type
      modifiers
```

Failures produce zero liberation pressure.

Optional failure side effects:

```text
perceived_difficulty += failure_reputation_bonus
actual_threat_score += enemy_confidence_bonus
```

Do not subtract liberation progress for failures unless the design explicitly
wants failed operations to lose territory.

## Pressure Computation

The current POC uses `diver_pressure` as a continuous rate. The next model
should replace or supplement that with completion-based pressure.

Suggested cell fields:

```python
pending_liberation: float       # successful completions waiting to be applied
recent_deployments: int         # social/enemy-response signal
active_missions: int            # in-flight players targeting this cell
actual_threat_score: float      # objective enemy response
perceived_difficulty: float     # player behavior estimate
```

Progress should use successful completion pressure, not active presence.

Option A: apply pressure immediately on completion.

```text
cell.progress += pressure_pulse
```

Option B: smooth pressure over a short window for nicer visualization.

```text
liberation_rate =
  pending_liberation / pressure_smoothing_window_s

cell.progress += liberation_rate * dt
cell.pending_liberation -= liberation_rate * dt
```

Option B keeps the map from jumping while preserving completion-based causality.

Enemy resistance can remain rate-based:

```text
net_progress_rate =
  liberation_rate
+ friendly_poi_rate
- enemy_resistance_rate
- enemy_poi_rate
+ base_rate
```

If using completion-only purity, `base_rate` should be zero or should represent
background successful operations generated by the population model.

## Success Probability

Success probability should depend on actual threat, perceived difficulty inputs,
cohort capability, and support.

Example:

```text
success_logit =
  base_success_logit
+ cohort_skill
+ friendly_support_bonus
+ population_support_bonus
+ artillery_support_bonus
- actual_threat_weight * actual_threat_score
- biome_failure_bias
- modifier_failure_bias
- fortress_penalty
```

Then:

```text
success_probability = sigmoid(success_logit)
```

Clamp to avoid impossible certainty:

```text
success_probability =
  clamp(success_probability, min_success_probability, max_success_probability)
```

## Completion Time

Completion time should be sampled by bucket, not by individual player.

Example:

```text
expected_completion_time_s =
  base_completion_time_s
* mission_type_duration_multiplier
* biome_duration_multiplier
* (1 + perceived_difficulty * difficulty_duration_scale)
* cohort_duration_multiplier
```

Add randomness:

```text
resolve_time =
  now
+ sample_lognormal(expected_completion_time_s, completion_time_variance)
```

For large buckets, split completion across a distribution instead of resolving
all players at one second:

```text
short bucket: one resolve time
large bucket: split into N sub-buckets across the sampled distribution
```

## Simulation Parameters

Initial parameter groups to add when this model is implemented.

### Allocation Parameters

```python
allocation_period_s: float = 1.0
allocation_temperature: float = 1.0
max_cells_considered_per_cohort: int = 64
switching_cost_decay_s: float = 300.0
social_gravity_scale: float = 1.0
congestion_capacity_per_cell: int = 5000
congestion_penalty_scale: float = 1.0
attention_friction_scale: float = 1.0
```

### Completion Parameters

```python
base_completion_time_s: float = 900.0
completion_time_variance: float = 0.25
large_bucket_split_size: int = 500
contribution_per_success: float = 1.0
pressure_smoothing_window_s: float = 10.0
min_success_probability: float = 0.05
max_success_probability: float = 0.98
```

### Difficulty Parameters

```python
actual_threat_weight: float = 1.0
visible_threat_weight: float = 0.6
biome_difficulty_weight: float = 0.25
mission_type_failure_weight: float = 0.25
modifier_reputation_weight: float = 0.15
recent_failure_rate_weight: float = 0.35
friendly_support_difficulty_reduction: float = 0.2
success_momentum_difficulty_reduction: float = 0.15
```

### Enemy Response Parameters

```python
deployment_response_weight: float = 0.4
completion_response_weight: float = 0.8
failure_confidence_bonus: float = 0.1
enemy_response_decay_s: float = 600.0
```

Deployments can alert the enemy, but successful completions should matter more
for strategic enemy response.

### Cohort Parameters

Each cohort should define utility weights and performance modifiers.

```python
@dataclass
class CohortParams:
    population_share: float
    strategic_value_weight: float
    reward_weight: float
    urgency_weight: float
    social_weight: float
    novelty_weight: float
    role_fit_weight: float
    momentum_weight: float
    difficulty_weight: float
    repetition_weight: float
    attention_friction_weight: float
    switching_cost_weight: float
    congestion_weight: float
    skill_bonus: float
    duration_multiplier: float
    preferred_challenge_level: float | None = None
```

## Telemetry for Real Players

Later, real completions should calibrate perceived difficulty and success
probability.

Log dimensions:

- planet
- cell
- biome
- enemy faction
- mission type
- difficulty tier
- modifiers
- actual threat score band
- perceived difficulty band
- fortress or resistance influence
- friendly support influence
- squad size
- operation duration
- success or failure
- disconnect or abandonment

Derived values:

```text
historical_failure_rate(context)
historical_completion_time(context)
expected_reward_per_minute(context)
perceived_difficulty_adjustment(context)
```

## Implementation Notes for Current POC

The current code has:

```python
Cell.diver_pressure
Cell.enemy_resistance
World._apply_pressure()
enemy_ai.update_enemy_pressure()
```

For the next iteration, avoid directly mapping active players to
`diver_pressure`. Either rename the field or introduce a separate completion
path:

```text
controller pressure       -> still useful for manual demos
simulated deployments     -> active_missions/recent_deployments only
successful completions    -> pending_liberation
pending_liberation        -> progress
```

A low-risk migration path:

1. Add `pending_liberation`, `active_missions`, `recent_deployments`,
   `actual_threat_score`, and `perceived_difficulty` to `Cell`.
2. Keep controller `diver_pressure` for manual v0 demos.
3. Add a population allocator that writes mission buckets.
4. Resolve mission buckets into `pending_liberation`.
5. Update `_apply_pressure()` to consume `pending_liberation` as the primary
   player contribution.
6. Later, deprecate or rename `diver_pressure` once the controller UI can drive
   simulated completions instead of direct pressure.

## Open Design Questions

- Should failed operations only produce zero progress, or can they increase
  enemy confidence and future actual threat?
- Should successful completions be applied instantly or smoothed over several
  seconds?
- Should actual threat respond more to deployments, successful completions, or
  both?
- Should congestion represent reduced strategic efficiency, matchmaking
  friction, enemy adaptation, or all three?
- How much of perceived difficulty should be shown directly to players versus
  inferred from UI signals?
