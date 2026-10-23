# Illustrative cost baseline

**Not a quote, invoice, current price lookup or spending ceiling.** These
illustrations retain public retail assumptions recorded for East US 2, USD,
on 2026-10-12. Reprice for your region/subscription and workload before deploying.
No billing history or current Azure inventory was queried for this refresh.

Warm workers are billable even when unused. A ready target of zero stops new
warm provisioning but does not itself delete existing groups or stop the
gateway/controller/shared infrastructure.

## Worker compute

Assumed confidential group shape: **3 vCPU / 5 GiB**, comprising a 2-vCPU/4-GiB
worker plus a 1-vCPU/1-GiB attestation sidecar.

| Historical retail assumption | Rate |
|---|---|
| Confidential ACI vCPU | $0.04455 / vCPU-hour |
| Confidential ACI memory | $0.004895 / GB-hour |
| Total per group | `3 * 0.04455 + 5 * 0.004895 = $0.158125/hour` |

| Continuously existing groups | Worker/hour | Worker/month (730 hours) |
|---|---:|---:|
| 0 | $0 | $0 |
| 2 (historical warm pilot target) | $0.31625 | $230.86 |
| 10 (scale illustration) | $1.58125 | $1,154.31 |
| 20 (cap illustration) | $3.16250 | $2,308.63 |

These are **average existing groups**, not completed sessions. Ten active plus
ten ready equals twenty billed groups. Startup/image-pull/drain time contributes;
destroy-after-use incurs repeated provisioning overhead.

## Shared infrastructure and control-plane allocation

The table below is one coherent **full allocated baseline**, not incremental
worker spend. It assumes a shared VPN, one resolver inbound endpoint, two public
IPs and three private endpoints. Your design may require different counts.

| Component | Assumed monthly USD |
|---|---:|
| Standard ACR | $20.28 |
| VpnGw1AZ, one gateway instance | $153.30 |
| Standard NAT fixed gateway charge | $32.85 |
| Two Standard public IPs | $7.30 |
| One DNS Private Resolver inbound endpoint | $180.00 |
| Three private endpoints at illustrative $0.01/hour | $21.90 |
| Shared fixed subtotal | **$415.63** |
| Gateway/controller compute, illustrative idle-to-active range | **$35.48-$118.29** |
| Shared + control subtotal | **$451.11-$533.92** |

Control-plane illustration assumes total 1.5 vCPU/3 GiB across the two apps.
Consumption active/idle billing depends on actual behavior and features; do not
assume the low end applies to a continuously active controller.
Private endpoint/resolver figures are planning allowances from the historical
source baseline, not freshly verified regional meters or confirmed inventory.
No unrelated shared model/monitor resources are assumed present.

| Existing groups | Worker/month | Full allocated baseline/month |
|---|---:|---:|
| 0 | $0 | $451.11-$533.92 |
| 2 | $230.86 | $681.97-$764.78 |
| 10 | $1,154.31 | $1,605.42-$1,688.23 |
| 20 | $2,308.63 | $2,759.74-$2,842.55 |

If these shared resources already serve other workloads, allocate a justified
fraction instead of counting them as free or double-counting them elsewhere.
The **incremental** service estimate may omit already-funded shared fixed costs;
label that accounting choice explicitly.

## Not included

Foundry model tokens/inference, request charges, logs/telemetry ingestion,
storage/capacity/transactions, NAT data processing, VPN egress, DNS zones/queries,
private-endpoint data processing, tax, discounts and currency conversion are
additional and workload-dependent.

An ACA environment management charge may apply depending on dedicated workload
profiles, private endpoints or other selected features. It is not included as an
unconditional base charge; confirm current feature billing. No HSM/SKR cost is
included because the pool does not use either.

Budgets/alerts are not hard caps. Total-cap enforcement bounds workers, not all
Azure costs, nor concurrent unrelated resources in a subscription. Confirm core
quota, capacity and create/delete throughput for destroy-after-use workloads.

## References for repricing

- [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
- [ACI pricing](https://azure.microsoft.com/pricing/details/container-instances/)
- [ACI billing timing and FAQ](https://learn.microsoft.com/azure/container-instances/container-instances-faq)
- [Container Apps billing](https://learn.microsoft.com/azure/container-apps/billing)
- [Private Link pricing](https://azure.microsoft.com/pricing/details/private-link/)
- [DNS pricing](https://azure.microsoft.com/pricing/details/dns/)
- [Design and limits](isolated-session-pool.md), [pilot operations](pilot-guide.md)
