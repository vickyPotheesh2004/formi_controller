# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.355 | +6.7% | 0.242 | +1.7% | 0.511 | +4.1% | 14% | 41 (47) |
| nw_kyc | 0.374 | +3.0% | 0.247 | +2.3% | 0.596 | +0.9% | 0% | 23 (21) |
| nw_renewal | 0.419 | -0.1% | 0.259 | -1.5% | 0.558 | +2.2% | 16% | 44 (42) |
| ALL | 0.381 | +3.4% | 0.249 | +0.6% | 0.544 | +2.7% | 12% | 108 (110) |

## Changes applied by the controller

25 applied — neutral: 21, beneficial: 3, harmful: 1

- nw_renewal v2 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v2 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.060 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v7 (controller_approved) dims=['prompt_version'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- nw_renewal v8 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_approved) dims=['welcome_message'] engageΔ=-0.033 didΔ=+0.000 → **harmful**
- ks_cancel v9 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v6 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.005 → **neutral**
- nw_renewal v10 (controller_approved) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- nw_kyc v7 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v11 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v8 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 16,
  "decisions": {
    "approved": 14,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 14,
  "auto_applied": 11,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "ks_cancel": 2,
    "nw_kyc": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.287,
    "4": 0.312,
    "5": 0.286,
    "6": 0.302,
    "7": 0.302
  }
}
```

Credits used: 9 · validation runs: 2 · LLM fix calls: 1