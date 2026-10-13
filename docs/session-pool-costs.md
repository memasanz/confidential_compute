# Confidential ACI Analysis-Session Pool — Pricing Research & Cost Note

## Updated full-platform planning baseline

Read-only inventory confirms Standard ACR,
Standard NAT, VpnGw1AZ, a VPN public IP in addition to the NAT public IP,
and one deployed DNS Private Resolver inbound endpoint. Attribute the full
shared VPN/resolver cost here, not only incremental pool spend.

| Monthly component (USD, 730 hours) | 10 workers | 20 workers |
|---|---:|---:|
| Confidential pool compute | 1,154.31 | 2,308.63 |
| Gateway/controller illustrative compute | 35.48-118.29 | 35.48-118.29 |
| Standard ACR | 20.28 | 20.28 |
| VpnGw1AZ | 153.30 | 153.30 |
| Standard NAT fixed charge | 32.85 | 32.85 |
| Two Standard public IPs | 7.30 | 7.30 |
| Existing DNS resolver inbound endpoint | 180.00 | 180.00 |
| Private endpoints allowance: new Table plus existing model/monitor connections | 21.90 | 21.90 |
| **Allocated infrastructure baseline** | **1,605-1,688** | **2,760-2,843** |

Math (10 workers): 1,154.31 + 20.28 + 153.30 + 32.85 + 7.30 + 180.00 + 21.90 = 1,569.94, plus gateway/controller 35.48 / 118.29 = 1,605.42 / 1,688.23. For 20 workers swap in 2,308.63 compute: 2,724.26 + 35.48 / 118.29 = 2,759.74 / 2,842.55.

Removed on 2026-10-13: the Managed HSM ($2,336.00/mo) and the legacy single-sandbox group ($115.43/mo). The previous baselines were 4,057-4,140 (10 workers) and 5,212-5,294 (20 workers); removing 2,451.43 gives the totals above. The pool needs no HSM (see [Why no HSM](isolated-session-pool.md#why-no-hsm)).

Private endpoints are $0.01/hour each; resolver endpoint reference price is
$180/month ([Private Link pricing](https://azure.microsoft.com/pricing/details/private-link/),
[DNS pricing](https://azure.microsoft.com/pricing/details/dns/)). The three-endpoint
allowance assumes the existing model/monitor private connections are used and
the planned Table endpoint is deployed. Other unrelated Fabric/Search/Cosmos
resources sharing the VNet are not part of this analysis-service estimate.

Add actual model inference, log ingestion, storage/transactions, DNS zones/queries,
network/data-processing and request usage. Those cannot be quoted as a fixed
monthly amount without workload volumes. These are retail estimates before tax,
discounts and free grants, not an actual invoice or spending ceiling. The
conditional ACA management charge described below is not included unless the
selected environment features trigger it. Ten active plus ten ready workers
is the twenty-worker scenario.

**Scope:** Public retail pricing research only — no Azure resources created/measured, no billing history queried, no repo/secret data used.
**Region:** East US 2 (`eastus2`) · **Currency:** USD · PAYG Consumption retail list price — no discounts/Reservations/Savings Plans/tax.
**Retrieved:** Oct 12, 2026, via Azure Retail Prices API (`api-version=2023-01-01-preview`), fully paginated, plus official Microsoft Learn docs.

## 1. Platform confirmation

Per [Confidential containers on ACI — overview](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-confidential-overview): confidential containers run "within a hardware-based and attested Trusted Execution Environment (TEE)" using "a Hyper-V isolated TEE... memory encryption key generated and managed by an AMD SEV-SNP capable processor." **Confirmed yes** — the TEE/attestation boundary is the **container group** (group-wide), matching one group per analysis session for mutual distrust; the docs name "data clean rooms for multi-party data analytics" as the fit-for-purpose scenario.

ACI has **no built-in autoscaling/pool controller**: *"Currently, scaling isn't available for containers or container groups... use our API to automate... more requests for container group creation"* ([ACI FAQ](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-faq)). The 20-cap/10-ready pool logic must live in the customer's own gateway/controller.

**Contrast, not substitution:** Azure Container Apps **"Dynamic sessions"** (its own billing construct, code-interpreter and custom-container pool types — [ACA billing](https://learn.microsoft.com/en-us/azure/container-apps/billing)) and the separate **`Microsoft.App/SandboxGroups` ("ACA Sandboxes")** resource type are **distinct ACA products from each other and from confidential ACI**. This note does not price Sandboxes and does not assert the Dynamic Sessions meter applies to Sandboxes, nor that either supports confidential/SEV-SNP TEE guarantees — no such claim exists in public docs.

## 2. Verified confidential ACI compute meters

Query `serviceName eq 'Container Instances' and armRegionName eq 'eastus2'` — complete result (16 items, `NextPageLink: null`, no pagination truncation):

| Meter | Rate | Unit | SKU/Product/SKU ID | Meter ID | Type | Effective |
|---|---|---|---|---|---|---|
| Confidential containers ACI vCPU Duration | **$0.04455** | 1 Hour/vCPU | Confidential containers ACI · DZH318Z0BQV6 · /0081 | `1c9dba36-b95d-5f5d-bcd0-bad6a229b967` | Consumption | 2024-09-01 |
| Confidential containers ACI Memory Duration | **$0.004895** | 1 GB-Hour | Confidential containers ACI · DZH318Z0BQV6 · /0081 | `c056b335-d681-5017-97b6-75b52a19a25f` | Consumption | 2024-09-01 |

For contrast only (not substituted): Standard (non-confidential) SKU is $0.0405/vCPU-hr and $0.00445/GB-hr — confidential carries a ~10% premium, matching FAQ: *"Confidential containers... do have an additional cost compared to standard SKU container groups."* GPU, Windows, Spot, and Dedicated-Hybrid meters from the same query were excluded as inapplicable.

**Per-group rate** (3 vCPU/5 GiB = 2 vCPU/4 GiB worker + 1 vCPU/1 GiB attestation sidecar):
```
(3 × $0.04455) + (5 × $0.004895) = $0.13365 + $0.024475 = $0.158125 / group-hour (compute only)
```

## 3. Worker-pool scenarios (formula: rate × hours × avg. concurrent groups)

Scenarios, **not guaranteed averages** — actual billed groups range 0–20 under the cap/ready policy, and billable time includes boot/image-pull and drain, not just active work. "10 active + 10 ready" = 20 billed groups, not 10.

| Scenario | Groups | Hours | Cost |
|---|---|---|---|
| Warm floor, continuous | 10 | 1 / 730 | **$1.5813/hr** / **$1,154.31/mo** |
| Illustrative average | 15 | 1 / 730 | **$2.3719/hr** / **$1,731.47/mo** |
| Cap, continuous | 20 | 1 / 730 | **$3.1625/hr** / **$2,308.63/mo** |
| Scheduled window | 20 | 176 | **$556.60/mo** |

730h = standard Azure monthly convention. **176h/month was supplied as an illustrative scenario parameter in the delegated research prompt — it is not a figure independently stated in the user's own requirements** and should be treated as a what-if, not a usage forecast. Figures above are worker-compute only; exclude gateway/controller/storage/networking (§4–§6).

## 4. Gateway/controller (ACA) — illustrative sizing only, not a measured requirement

Sizes (gateway 1 vCPU/2 GiB, controller 0.5 vCPU/1 GiB, min-replica 1 each) are **proposed for this estimate only**.

| Meter | Rate | Effective |
|---|---|---|
| Standard vCPU Active Usage | $0.000024/vCPU-sec ($0.0864/hr) | 2022-06-01 |
| Standard vCPU Idle Usage | $0.000003/vCPU-sec ($0.0108/hr) | 2022-06-01 |
| Standard Memory Active Usage | $0.000003/GiB-sec ($0.0108/hr) | 2022-06-01 |
| Standard Memory Idle Usage | $0.000003/GiB-sec ($0.0108/hr) | 2022-06-01 |

**Correction verified:** only **vCPU** active-vs-idle carries an 8× premium ($0.000024 vs $0.000003/sec). **Memory Active and Idle are identically priced** ($0.000003/GiB-sec both) — there is no memory-side idle discount, confirmed directly from the API.

Illustrative monthly compute range (730h, idle floor → active ceiling): gateway ≈ $23.65–$78.86, controller ≈ $11.83–$39.43.

**Environment Management Hour ($0.10/hr) — conditional, not an unconditional base cost.** Per [Billing in Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/billing): *"you aren't billed any plan management charges unless you use a Dedicated workload profile in your environment"*; however *"features such as private endpoints and planned maintenance are subject to a Dedicated Plan Management charge regardless of whether you use the Consumption or Dedicated plans."* A plain internal-VNet-integrated **Consumption**-only environment with **no** Dedicated workload profile and **no** environment-level private endpoint should **not** incur this charge; it applies **only if** a private endpoint is attached to the environment or a Dedicated profile/planned-maintenance feature is used. Retail-meter existence alone does not establish billing applicability — flagged per this correction. If a private endpoint is later added to the environment, budget $0.10/hr (≈$73/mo) per the `Environment Private Endpoint` meter (`b564ef7d-...`).

Request charges ($0.40/million Standard Requests) are rate-verified but not estimated — no traffic volume given.

## 5. Billing timing & quota (official docs)

- **Meter start:** *"duration is calculated from the time that we start to pull your first container's image... until the container group is stopped."* **Meter stop:** *"Meters stop running once your entire container group is stopped."* ([ACI FAQ](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-faq)) — image pull/boot is billable; with destroy-not-recycle workers, billed window per worker = image-pull-start → stop/delete.
- Public default **changeable** quota: *"Standard sku cores (CPUs) per region per subscription | 100"* ([quota limits](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-resource-and-quota-limits)); docs don't publish a separate confidential-sku core default — Microsoft's guidance is to check via the List Usage API. The user's own verified 100-core/63-used figure is accepted as-is and not independently re-derived here.
- Confidential per-group ceiling: 31 vCPU/180 GB (quota page) vs. FAQ's "32 vCPU/192 GB" (big containers) — minor cross-doc inconsistency, immaterial at 3 vCPU/5 GiB/group.
- Group create/delete default throughput: 300/hour (configurable) — relevant to churn under destroy-not-recycle.

## 6. Table Storage (leases/jobs, Standard LRS)

| Meter | Rate | Unit |
|---|---|---|
| LRS Data Stored | $0.045 | GB/month |
| Read / Write / Batch Write Ops | $0.00036 | per 10,000 |

At plausible small volumes (≤1GB, ≤1M ops/mo) this is **well under $1/month** — unit rates shown, no volume assumed/fabricated.

## 7. Networking — verified fixed rates for the actual probed SKUs, gaps still disclosed

Probed SKUs (read-only, public-rate lookup only; no billing/portal data accessed): NAT `natgw-aci` (Standard) + Public IP `pip-aci-nat` (Standard static), VPN Gateway `VpnGw1AZ` (`activeActive: false` — billed as **one** gateway instance; the `capacity: 2` field is a scale-unit/throughput attribute, **not** two billed gateway units).

| Resource | Meter | Rate (verified) | Monthly ref. (730h) |
|---|---|---|---|
| NAT Gateway, Standard | Standard Gateway (fixed) | $0.045/hour (Global meter) | **$32.85** |
| NAT Gateway, Standard | Standard Data Processed | $0.045/GB | usage-dependent — **no volume given, not quantified** |
| Public IP `pip-aci-nat`, Standard static | Standard IPv4 Static Public IP | $0.005/hour | **$3.65** |
| VPN Gateway `VpnGw1AZ` | VpnGw1AZ (single instance) | $0.21/hour | **$153.30** |

- **Private DNS** (global meters): zone $0.50/mo (first 25), record sets free to 25,000 then $0.0015/mo, queries $0.40/M — a new/reused zone here is trivial (~$0.50/mo).
- **DNS Private Resolver endpoint** (only if actually provisioned — not confirmed as part of this architecture): Inbound/Outbound Endpoint **$180/month each**, DNS Forwarding Ruleset **$2.50/month** — identical price across every pricing zone found (Zone 1–4, US Gov Zone 1–2), so stated with reasonable confidence despite the `eastus2`→zone mapping itself not being independently confirmed.
- **Private Endpoint (Storage account) hourly/data-processing meter: still not located.** Re-checked this session under `Virtual Network`, `Storage`, and the `Virtual Network Private Link` product specifically — that product's "Standard Private Endpoint" ($0.01/hr) and data-processed meters exist **only** for ExpressRoute/ER-peering edge locations (`attatlanta1`, `sgxsingapore1`, `US Gov`, `portland`, `perth`) and **not** for standard Azure regions like `eastus2`. **No regional Storage-account private-endpoint rate is confirmed — label stays unconfirmed, not quantified.**
- **TLS/PKI:** no new cost if an existing cert/mechanism is reused; not estimated if a new one is introduced (none specified).

## 8. Existing shared fixed costs — reused/already running, **not free**, excluded from incremental pool total

| Resource | Rate (verified) | Monthly ref. (730h) |
|---|---|---|
| ACR Standard | $0.6666/day | **≈ $20.28** |
| NAT Gateway Standard (fixed gateway charge only) | $0.045/hour | **$32.85** |
| Public IP Standard static | $0.005/hour | **$3.65** |
| VPN Gateway VpnGw1AZ (single instance) | $0.21/hour | **$153.30** |
| **Existing shared fixed subtotal** (20.28 + 32.85 + 3.65 + 153.30) | | **≈ $210.08/month** |

Explicitly **not included** in this subtotal (usage-dependent, no volume given, not fabricated): NAT Standard Data Processed ($0.045/GB), VPN Gateway egress/data-transfer, ACR data stored beyond base unit, and any Log Analytics/App Insights ingestion. This subtotal is shown only as a transparent reference baseline — **not summed into §3/§4/§6**, to avoid double counting.

## 8a. All-in monthly total (incremental pool + existing shared fixed baseline)

**Formula:** `All-in = Incremental pool (§3 compute + §4 gateway/controller + §6/§7 trivial storage/DNS) + Existing shared fixed baseline (§8)`, **excluding** every usage-dependent item above (none quantified, none fabricated) and the unconfirmed Storage Private Endpoint rate.

| Scenario | Incremental pool (compute + gw/controller + trivial) | + Existing shared fixed (§8) | **All-in total/month** |
|---|---|---|---|
| 10 groups continuous | ≈ $1,190 – $1,274 | $210.08 | **≈ $1,400 – $1,484** |
| 20 groups continuous (cap) | ≈ $2,344 – $2,428 | $210.08 | **≈ $2,554 – $2,638** |

**This is a bounded fixed-rate baseline, not a complete bill.** Still unquantified on top of the above: NAT/VPN data-processed GB, ACA request volume, any Log Analytics/App Insights ingestion, and the unconfirmed Storage private-endpoint rate — all usage-dependent and explicitly not estimated due to no volume data. Confidential pool compute is now the largest line item.

## 9. Excluded

Azure OpenAI inference, Copilot credits, tax/currency/discounts, and all actual billing-account history (none queried).

## 10. Open verification gaps

- **Private Endpoint (Storage account)** hourly/data-processing rate — still not found; the only "Private Link" product in the Retail API is ExpressRoute-edge-zone-scoped, not regional.
- **Environment Management Hour** applicability for the actual chosen ACA environment config — verify against final workload-profile/private-endpoint decision before costing it in.
- **Usage-dependent volumes** (NAT data processed, VPN egress, ACA request count, Log Analytics/App Insights ingestion) — rates partly verified, **no volume data given, nothing estimated**.
- **DNS Private Resolver** — priced ($180/mo/endpoint) only if this component is actually part of the architecture; not confirmed either way here.

### Sources
Azure Retail Prices API (`prices.azure.com/api/retail/prices?api-version=2023-01-01-preview`) · [ACI confidential overview](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-confidential-overview) · [ACI quota limits](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-resource-and-quota-limits) · [ACI FAQ](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-faq) · [ACA Billing](https://learn.microsoft.com/en-us/azure/container-apps/billing)
