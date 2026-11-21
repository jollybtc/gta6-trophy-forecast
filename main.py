"""GTA VI trophy rarity forecaster (gta6-trophy-rarity-forecast-01).

Reads one case at a time from MAKEFUN_INPUT_DIR (default /input), writes
<id>.out to MAKEFUN_OUTPUT_DIR (default /output).

Model, in short (see STRATEGY.md): work in COUNTS, not rates.  A trophy's
holders E_i = rate_i * N / 100, where N is Exophase's players-tracked.
When N jumps at a refresh, rates fall by dilution even though nobody
un-earned anything; new joiners bring their own (low) trophy prevalence,
and the existing cohort slowly earns more.  Forecast N at the target with
a damped log-growth fit, forecast each E_i from joiner prevalence plus
existing-cohort progress, convert back to a rate, and put quantiles on
the logit scale with a spread that grows with the horizon and with how
little the history actually moved.

Stdlib only, no network, deterministic.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0, 0.674, 1.645]


def clamp_pct(x):
    return min(99.99, max(0.01, x))


def logit(pct):
    p = clamp_pct(pct) / 100.0
    return math.log(p / (1.0 - p))


def inv_logit(x):
    if x >= 0:
        e = math.exp(-x)
        return 100.0 / (1.0 + e)
    e = math.exp(x)
    return 100.0 * e / (1.0 + e)


def round4(x):
    scaled = x * 10000
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 10000
    return int(value) if value == int(value) else value


def parse_time(s):
    # ISO-8601, e.g. 2026-11-19T03:00:00Z or with fractional seconds.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def counts(rate, n):
    """Holders implied by a 2dp rate, rounded to the nearest whole player."""
    if rate is None:
        return None
    return int(math.floor(rate * n / 100.0 + 0.5))


def distinct_reads(history):
    """Collapse consecutive identical reads (same N and same rates)."""
    out = []
    for r in history:
        key = (r.get("players_tracked"), tuple(r.get("rates") or []))
        if out and out[-1][0] == key:
            continue
        out.append((key, r))
    return [r for _, r in out]


def forecast_case(case, name):
    if case.get("levels") != LEVELS:
        raise ValueError(f"{name} asks for levels {case.get('levels')}, not {LEVELS}")
    history = case.get("history") or []
    if not history:
        raise ValueError(f"{name} has no history")
    trophies = case["trophies"]
    n_tr = len(trophies)
    horizon_h = float(case["horizon_minutes"]) / 60.0
    horizon_d = horizon_h / 24.0

    # Last known rate per trophy (walk backwards over nulls).
    last_rate = []
    for i in range(n_tr):
        v = None
        for r in reversed(history):
            rates = r.get("rates") or []
            if i < len(rates) and rates[i] is not None:
                v = float(rates[i])
                break
        last_rate.append(v if v is not None else 0.5)

    last = history[-1]
    n_last = float(last["players_tracked"])
    e_last = []
    for i in range(n_tr):
        e = counts(last_rate[i], int(n_last))
        e_last.append(float(e if e is not None else 0))

    distinct = distinct_reads(history)
    steps = []  # (dt_h, dN, [dE_i])
    for a, b in zip(distinct, distinct[1:]):
        dt = (parse_time(b["at"]) - parse_time(a["at"])).total_seconds() / 3600.0
        if dt <= 0:
            continue
        na = float(a["players_tracked"])
        nb = float(b["players_tracked"])
        d_e = []
        for i in range(n_tr):
            ra = (a.get("rates") or [None] * n_tr)[i] if i < len(a.get("rates") or []) else None
            rb = (b.get("rates") or [None] * n_tr)[i] if i < len(b.get("rates") or []) else None
            ea = counts(ra, int(na)) if ra is not None else None
            eb = counts(rb, int(nb)) if rb is not None else None
            if ea is None or eb is None:
                d_e.append(0.0)
            else:
                d_e.append(float(eb - ea))
        steps.append((dt, nb - na, d_e, na, nb))

    total_dn = sum(s[1] for s in steps if s[1] > 0)
    total_de = [sum(s[2][i] for s in steps) for i in range(n_tr)]

    # ---- joiner prevalence q_i: pooled dE/dN, shrunk toward a prior ----
    # Prior: joiners hold ~0.032 trophies each besides the 100% trophy,
    # spread in proportion to each trophy's current rate (early/story
    # trophies first). The 100% trophy's joiners hold it at rate 1.
    sum_rates_other = sum(last_rate[i] for i in range(n_tr) if last_rate[i] < 99.99)
    alpha = 300.0  # pseudo-joiners of shrinkage
    q = []
    for i in range(n_tr):
        if last_rate[i] >= 99.99:
            q.append(1.0)
            continue
        prior = 0.032 * (last_rate[i] / sum_rates_other) if sum_rates_other > 0 else 0.0006
        if total_dn > 0:
            raw = total_de[i] / total_dn
            raw = min(1.0, max(0.0, raw))
            q.append((total_de[i] + alpha * prior) / (total_dn + alpha))
        else:
            q.append(prior)

    # ---- existing-cohort progress: residual earning per player per hour ----
    # Residual after the joiner term, pooled over steps, shrunk toward a
    # small prior that scales with the rate itself (a trophy almost nobody
    # has is earned by almost nobody per hour; the platinum, which needs
    # every other trophy, slower still). The prior is deliberately small:
    # where the history has real steps the fitted value dominates, and
    # where it has none a big prior would invent progress that never
    # happened. Progress is also damped over the horizon (tau 48 h):
    # players earn fastest in their first hours, not at a constant rate.
    prog = []
    for i in range(n_tr):
        if last_rate[i] >= 99.99:
            prog.append(0.0)
            continue
        prior = min(0.0002, 0.0003 * (last_rate[i] / 100.0))
        num = 0.0
        den = 0.0
        for dt, dn, d_e, na, nb in steps:
            if dt <= 0:
                continue
            resid = d_e[i] - q[i] * max(0.0, dn)
            num += resid
            den += na * dt
        if den > 0:
            raw = num / den
            raw = min(0.01, max(0.0, raw))
            # Shrink: pseudo-denominator of 50k player-hours.
            prog.append((num + 50000.0 * prior) / (den + 50000.0))
        else:
            prog.append(prior)
    prog_h = 48.0 * math.log(1.0 + horizon_h / 48.0) if horizon_h > 0 else 0.0

    # ---- forecast N(target) ----
    if total_dn <= 0:
        # Flat history: no refresh has happened yet in the window. Assume
        # one refresh before the target, sized like the launch-night one
        # (N 638 -> 3868, a factor of ~6.06), scaled in by horizon: a
        # 90-minute horizon gets the whole refresh, shorter gets part.
        factor = 1.0 + 5.06 * min(1.0, horizon_h / 1.5)
        n_target = n_last * factor
        flat = True
    else:
        flat = False
        # Two damped extrapolations of N, take the smaller (conservative:
        # over-forecasting N inflates both the joiner and progress terms,
        # and early growth must never be extrapolated unchecked).
        # (a) Power law: log N linear in ln(1 + hours since first read),
        # least squares over distinct reads, slope clipped to [0, 2].
        t0 = parse_time(distinct[0]["at"])
        xs = []
        ys = []
        for r in distinct:
            h = (parse_time(r["at"]) - t0).total_seconds() / 3600.0
            xs.append(math.log(1.0 + max(0.0, h)))
            ys.append(math.log(float(r["players_tracked"])))
        n_d = len(xs)
        mx = sum(xs) / n_d
        my = sum(ys) / n_d
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
        slope = min(2.0, max(0.0, slope))
        intercept = my - slope * mx
        h_target = (parse_time(case["target"]) - t0).total_seconds() / 3600.0 if case.get("target") else horizon_h
        n_power = math.exp(intercept + slope * math.log(1.0 + max(0.0, h_target)))
        # (b) Recent growth rate, damped with tau = 24 h.
        g_recent = 0.0
        for dt, dn, d_e, na, nb in reversed(steps):
            if dt > 0 and na > 0 and nb > 0 and dn > 0:
                g_recent = math.log(nb / na) / dt
                break
        g_recent = min(g_recent, 1.0)
        n_damped = n_last * math.exp(g_recent * 24.0 * math.log(1.0 + horizon_h / 24.0))
        # Horizon-aware cap: at most ~1 + 2.5*sqrt(days) times the last N.
        cap_mult = 1.0 + 2.5 * math.sqrt(horizon_d)
        n_target = min(n_power, n_damped, n_last * cap_mult)
        n_target = max(n_target, n_last)

    n_avg = 0.5 * (n_last + n_target)

    # ---- spread on the logit scale ----
    # Horizon-driven: a 90-minute practice horizon is genuinely quite
    # predictable once dilution is modelled (the median is already within
    # ~0.1 logit of every plausible answer), so a wide band only pays
    # pinball at every quantile. The 4.5-day final horizon is not: there
    # the spread must be wide. Flat histories and histories with almost
    # no steps get a small extra allowance for the refresh itself.
    sigma = 0.30 + 0.40 * math.sqrt(horizon_d)
    if flat:
        sigma += 0.10
    if len(steps) < 3:
        sigma += 0.08
    sigma = min(1.6, max(0.35, sigma))

    rows = []
    for i, trophy in enumerate(trophies):
        if last_rate[i] >= 99.99:
            rows.append({"id": trophy["id"], "q": [100, 100, 100, 100, 100]})
            continue
        e_pred = (
            e_last[i]
            + q[i] * max(0.0, n_target - n_last)
            + prog[i] * n_avg * prog_h
        )
        e_pred = min(max(e_pred, 0.0), n_target)
        med = 100.0 * e_pred / n_target if n_target > 0 else last_rate[i]
        med = min(99.99, max(0.01, med))
        lg = logit(med)
        qs = []
        for z in Z:
            v = inv_logit(lg + z * sigma)
            qs.append(round4(min(99.99, max(0.01, v))))
        # Keep non-decreasing after rounding.
        for k in range(1, 5):
            if qs[k] < qs[k - 1]:
                qs[k] = qs[k - 1]
        rows.append({"id": trophy["id"], "q": qs})
    return {"trophies": rows}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
