# H1 hosting evidence package (OQ-H1 to OQ-H4, gate G-9)

Checklist for Arnav to complete. Written Oct 28, 2026 against `main` at
`efdb5a57cf22ec99c4fe69f228e341fe6dde52bc`.

> **G-9 status: NOT MET. Pending Arnav's account evidence.** Nothing here shows that any
> account is eligible, that any use is permitted, that hosting costs ₹0, or that
> exhaustion is acceptable. Public documentation describes the offers in general. It
> cannot show Arnav's eligibility, subscription settings or payment exposure, and it
> cannot show that no chargeable resource exists. G-9 also needs the deployment plan to
> keep exposure within the G-4 bounds, and **no G-4 bound exists yet**. So G-9 cannot pass
> before G-4 records evidence, whatever the account checks show.

**Do not create any cloud resource** until every charge-risk row in
[section 4](#4-decision-table) is `pass` for that candidate and Arnav has recorded the
decision.

Contents:

1. [How to collect and keep evidence](#1-how-to-collect-and-keep-evidence)
2. [Account questions by open question](#2-account-questions)
3. [Official documentation checked](#3-official-documentation-checked-oct-28-2026)
4. [Decision table](#4-decision-table)
5. [Unresolved items](#5-unresolved-items)

## 1. How to collect and keep evidence

**Evidence types.** Every answer records which kind of evidence it rests on:

| Code | Kind | What it can show |
| --- | --- | --- |
| **A** | Account observation: a dated screenshot or export from Arnav's own portal, signed in | The state of *this* account or subscription on that date |
| **P** | Provider-wide public documentation or terms (section 3) | What the provider says applies in general. Never Arnav's own state |
| **S** | Written provider confirmation: a support ticket reply or an email from the provider | The provider's answer to a specific question about this account or use |

A **P** source never answers an account question. Where the terms are ambiguous, the
answer is an **S** request, not an inference.

**Where to keep it.**

- Keep evidence **outside this repository**, for example in a private folder in Arnav's
  own cloud drive. Do not commit screenshots, billing records or support replies, not
  even to a private branch.
- Name files `YYYY-MM-DD_<provider>_<ref>.<ext>` (for example
  `2026-10-29_azure_E-AZ-04.png`), using the evidence references below.
- Keep a private index (date, ref, which page, which account and subscription,
  what it shows). Only `pass`/`fail`/`unknown`, the date and the ref go into
  [section 4](#4-decision-table) or a PR.

**Redaction before keeping or sharing anything.**

- Never share or capture a password, MFA or recovery code, API key, SSH private key or
  SIG key. No agent ever receives any of these.
- Never capture a full card number, CVV, bank details, billing address or phone number.
  If a payment method is shown, keep only its type and the last 4 digits.
- Partially mask the subscription ID or tenancy OCID (keep the first and last 4
  characters) and the account email. Keep the offer name, offer ID, dates and amounts
  visible: those are the evidence.
- Every screenshot must show the **date** and the **account or subscription context**
  (the subscription or tenancy name in the portal header or breadcrumb). A screenshot
  without both is not evidence.

## 2. Account questions

The design choices referred to below are in [`design.md`](design.md#7-proposed-cloud-resources-per-candidate).
In short:

- **Azure:** one Linux VM of a SKU listed as free for this subscription (proposed
  `Standard_B1s`, alternatives `B2ats v2` / `B2pts v2`), one OS disk, no data disk, no
  snapshots, one public IPv4, a network security group allowing SSH only, and no paid
  add-ons (Bastion, NAT gateway, load balancer, Log Analytics, Monitor agent, Backup,
  Defender plans).
- **Oracle:** one Always Free VM (`VM.Standard.E2.1.Micro` or a small `VM.Standard.A1.Flex`)
  in the home region, with its boot volume and included public IP.

### OQ-H1: Eligibility and permitted use

Azure for Students first:

| Ref | Question | Evidence to collect | Type |
| --- | --- | --- | --- |
| E-AZ-01 | Does Arnav hold an active **Azure for Students** subscription, and what is its offer ID? The public offer page lists it as `MS-AZR-0170P`. | Azure portal, Subscriptions → the subscription → Overview: offer name, offer ID, status (Active, Disabled or Warned) | A |
| E-AZ-02 | On what date was the subscription created, and when does the current 12-month credit period end? | Sponsorships portal balance page (<https://www.microsoftazuresponsorships.com/balance>): the expiry date shown below the credit chart | A |
| E-AZ-03 | Does the credit expire after **Nov 4, 2026, 10:30pm IST**, with a margin of at least a few days? If the anniversary falls inside the competition window, has a renewal notice arrived, and what happens to running resources at renewal? | E-AZ-02 date. Any renewal email (redacted). If the anniversary is close, a support reply on renewal behaviour | A, S |
| E-AZ-04 | Was student status verified, and is the subscription in good standing (no suspension, no verification hold)? | Education hub or Sponsorships portal status page | A |
| E-AZ-05 | **Permitted use.** Do the offer terms allow running an automated trading bot, unattended and 24/7, that trades simulated competition currency (SUSQies) through a third party's public API for a student competition (the SIG Predictions Cup)? The offer page's "Prohibited Use" list names cryptocurrency mining, denial-of-service, spamming and illegal activity. It does not mention this use, and it does not explicitly allow or forbid running a service. **Silence is not permission.** | A support request, or an email to the Azure for Students team, quoting this exact use. Keep the reply. Also save the offer terms page as shown on the date of the check (PDF print) | S, P |
| E-AZ-06 | Does any Azure Policy on the subscription restrict regions or SKUs? Student subscriptions are reported to carry an allowed-regions policy; this is unverified. | Portal → Policy → Assignments for the subscription (screenshot). Alternatively, the region list offered in the VM creation form, *without* clicking Create | A |

Oracle (only if Azure is rejected; see OQ-H4):

| Ref | Question | Evidence to collect | Type |
| --- | --- | --- | --- |
| E-OC-01 | Does Arnav already have an Oracle Cloud tenancy? If so, what are its account type (Free Tier, trial, or upgraded Pay As You Go), its home region and its trial status? | OCI Console → Billing & Cost Management → Subscriptions or Account management: account type and trial end date. Tenancy details: home region | A |
| E-OC-02 | Permitted use: the same use as E-AZ-05, against the Oracle Cloud Services Agreement and Acceptable Use Policy in force for the tenancy | A written reply from Oracle Support or Sales, and saved copies of the agreement and policy pages | S, P |

### OQ-H2: Allowance, quotas, billing type and payment exposure

| Ref | Question | Evidence to collect | Type |
| --- | --- | --- | --- |
| E-AZ-07 | How much credit remains, and how much has been used, by service? | Sponsorships portal: Balance and Usage pages | A |
| E-AZ-08 | Which **free services** does this subscription show, with their remaining monthly quantities? In particular: the proposed VM SKU (750 hours/month?), the managed disk tier and size (P6 64 GiB?), snapshots, public IPv4 hours, outbound bandwidth. The public students page lists B1s, B2pts v2 and B2ats v2 at 750 hours for 12 months. It does not show whether this subscription gets them, or whether the default OS disk size is covered. | Portal → search "Free services": the full list with usage (screenshot or CSV export) | A |
| E-AZ-09 | For each resource in the proposed design: is it free, or is it billed against credit? At what rate does it draw on the credit? Expected items include the public IPv4 (billed hourly on the public pricing page; any free allowance for this subscription is unverified), a disk outside the free tier, boot diagnostics storage, and outbound data beyond any free amount | The pricing calculator, with the result saved, *and* E-AZ-08. After any creation, cost analysis must show the actual meters (that check belongs to the runbook, not to this decision) | A, P |
| E-AZ-10 | Projected credit use from creation to Nov 5, 2026, with margin. Is it less than the remaining credit (E-AZ-07)? | A short calculation in the private index: each item × hours × rate, against E-AZ-07 | A |
| E-AZ-11 | **Billing type:** is the subscription still the Azure for Students offer, not upgraded to Pay-As-You-Go? Which billing account and agreement type is it under (MOSP or MCA)? | Cost Management + Billing → Billing scopes; subscription Overview → Offer | A |
| E-AZ-12 | **Payment method:** is any credit card or other payment method attached to the billing account or profile? A payment method is what allows the spending limit to be removed. | Cost Management + Billing → Payment methods (type and last 4 digits only; screenshot with the empty list if there is none) | A |
| E-AZ-13 | **Spending limit:** is it shown as **On**? Has it ever been removed? Is any banner offering to remove it? | Subscription Overview (spending limit status), Cost Management + Billing | A |
| E-AZ-14 | Are any Marketplace, third-party or separately billed products active, and are any purchases enabled? The spending limit does not stop these charges (spending-limit doc, section "A spending limit doesn't prevent all charges"). | Cost Management → Cost analysis grouped by publisher or service; the list of Marketplace purchases (should be empty) | A |
| E-AZ-15 | Compute quotas: is the regional vCPU quota for the proposed SKU family above zero in the chosen region? Is the total regional vCPU quota above zero? | Subscription → Usage + quotas, filtered to the region | A |
| E-AZ-16 | Which notifications go where: budget alerts, the spending-limit email, credit-expiry emails? | Cost Management → Budgets (a budget can be *created* without creating resources); the notification contact on the account | A |
| E-OC-03 | Oracle: is a payment card on file? (Oracle sign-up normally requires one.) Does the account stay **Free Tier, not upgraded**? Oracle's docs say the card "will not be charged unless you upgrade". | Billing → Payment method (type and last 4 only); account type (E-OC-01); a written Oracle confirmation that a non-upgraded account cannot incur charges | A, S |
| E-OC-04 | Oracle: are Always Free shapes and capacity available in the home region? The limits differ between pages: 1,500 OCPU-hours / 9,000 GB-hours (2 OCPU, 12 GB) on the resources page, 4 OCPU / 24 GB in an older Free Tier snippet. | Console → Limits, Quotas and Usage for the tenancy: A1 and E2 Micro limits | A |

### OQ-H3: What happens on exhaustion, expiry, suspension or deallocation

For each event, answer for each of: **VM state**, **OS/boot disk**, **data disks**,
**snapshots and backups**, **public IP** (kept or released, and whether it changes),
**monitoring and boot diagnostics**, **logs on the VM**, **any charge**, **how and when
service resumes**.

| Ref | Event (Azure) | Documented, provider-wide (P) | Still needs account or written evidence |
| --- | --- | --- | --- |
| E-AZ-17 | Credit used up (spending limit reached) | Spending-limit doc: services are disabled; VMs are "stopped and de-allocated"; storage data is "available as read-only"; settings "like virtual IP addresses" are lost when resources are deallocated. Students doc: once credit runs out, "Azure disables your services and subscription". | Whether disks are kept, for how long, and whether they are deleted later. Whether the VM gets a guest shutdown signal before deallocation. Whether anything can be charged. Whether a disabled student subscription re-enables on its own: the spending-limit doc's auto re-enable applies to offers with *monthly* credit, while Azure for Students credit is a 12-month total. **Written confirmation (S)** |
| E-AZ-18 | 12-month credit expiry | Students doc: "Once your credit expires, Azure disables your subscription"; to continue, upgrade to pay-as-you-go through support | Retention period and the fate of disks and IPs after disablement (no P source found). **S** |
| E-AZ-19 | Suspension (verification, suspicious activity, offer ended) | Offer terms: Microsoft "may suspend or terminate" access without notice, and the offer is limited "while supplies last" | What happens to resources. **S** |
| E-AZ-20 | Deallocation by Arnav, or a platform stop | VM states doc: a deallocated VM is not billed for compute, but "Disks and Networking continue to incur charges". IP pricing page: no charge for *dynamic* public IPs on stopped-deallocated VMs, which implies a dynamic IP may change | Which IP SKU and allocation the proposed design uses, and whether its charge is covered (E-AZ-08/09) |
| E-AZ-21 | 90 days of inactivity | Offer terms: Microsoft may remove compute resources after more than 90 days without use or charges, after notice | Not expected to apply before Nov 4. Record only |
| E-AZ-22 | Upgrade (Arnav's own action, or support's) | Disabled-subscription doc: if the subscription is upgraded after being disabled, resources are re-enabled and charged | **Never upgrade.** An upgrade to Pay-As-You-Go breaks REQ-4. Record that no upgrade has been requested |

| Ref | Event (Oracle) | Documented, provider-wide (P) | Still needs evidence |
| --- | --- | --- | --- |
| E-OC-05 | Trial ends (30 days or $300) | Free Tier doc: Always Free resources continue "without interruption"; paid resources are "reclaimed by Oracle unless you upgrade"; A1 instances above the Always Free allowance are disabled, then deleted after 30 days | Confirmation that the proposed VM is Always Free-eligible in the home region (A) |
| E-OC-06 | **Idle reclamation** | Always Free resources page: instances may be reclaimed if, over 7 days, 95th-percentile CPU is under 20%, network is under 20%, and (A1 only) memory is under 20% | A dry-run engine is likely to look idle by these measures. Whether reclamation deletes or stops the instance, whether notice is given, and what happens to the boot volume. **S** |
| E-OC-07 | Out of host capacity | Always Free resources page: Always Free shapes may be temporarily unavailable | Whether a running instance can lose capacity, or only creation fails (S) |

### OQ-H4: Oracle as a conditional candidate

Evaluate Oracle **only if** Azure is rejected on a recorded `fail`. Use the same rows (E-OC-01
to E-OC-07) and the same reject conditions. Oracle is **not** an automatic fallback.

- Creating a new tenancy requires a card, and starts a $300 trial. Resources created
  during the trial that are not Always Free are reclaimed at the end of the trial, which
  would be an outage.
- The idle-reclamation policy (E-OC-06) is a direct outage risk for a low-load dry-run
  engine. The workaround of generating load to avoid looking idle is not proposed. It
  would need Oracle's written view first, because it may conflict with the terms.

## 3. Official documentation checked (Oct 28, 2026)

All pages were opened on **Oct 28, 2026**. "Page date" is the date the page itself shows
(`ms.date` or updated date), where one is given. Re-check every link on the day Arnav
records a decision: offers change without notice.

| # | Source | Page date | Supports (provider-wide only) | Ambiguity or gap |
| --- | --- | --- | --- | --- |
| P-AZ-1 | [Azure for Students offer](https://azure.microsoft.com/en-us/free/students) | none shown | $100 credit "within 12 months"; no card at sign-up; 750 h each of B1s, B2pts v2 and B2ats v2 for 12 months; once credit is used up the subscription is disabled unless moved to pay-as-you-go; one account per person | Eligibility footnote text not captured. Does not say whether every student subscription gets the 12-month free services |
| P-AZ-2 | [Offer terms MS-AZR-0170P](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p) | none shown | Age 18 or older, full-time student, school-email verification; one subscription per person; activations limited; credit does not roll over; subscription disabled when credit is used up; prohibited uses (mining, DoS, spam, illegal); possible suspension; removal after 90 days inactive | Silent on automated bots and competitions. Silent on data retention after disablement. Refers to "this Agreement" without naming or linking it |
| P-AZ-3 | [Azure for Students FAQ](https://azure.microsoft.com/en-us/free/free-account-students-faq) | none shown | Same eligibility; no card; disabled when credits are exhausted; upgrade through support | No renewal detail; no data-retention detail; does not list free-service quantities |
| P-AZ-4 | [Reactivate a disabled Azure for Students subscription](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/azurestudents-subscription-disabled) | Jun 25, 2026 | Usage beyond free services draws on credit; "Azure disables your services and subscription" when credit runs out; credit expires at 12 months; the Sponsorships portal shows the balance and expiry | No resource or data fate; no grace period |
| P-AZ-5 | [Azure spending limit](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/spending-limit) | Apr 29, 2026 | On reaching the limit, VMs are stopped and deallocated and storage is read-only; IP settings are lost; removing the limit needs a payment method; the limit **does not prevent** Marketplace and some external charges | Examples are written for the Free Trial and Visual Studio offers. Not confirmed for Azure for Students. Auto re-enable described for monthly-credit offers |
| P-AZ-6 | [Reactivate a disabled subscription](https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/subscription-disabled) | Jun 7, 2026 | Disabled when credit expires or the limit is reached; upgrading re-enables resources and charges for them | General, not student-specific |
| P-AZ-7 | [VM states and billing](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing) | Oct 31, 2023 (updated Jun 24, 2026) | Stopped (allocated) is billed; deallocated is not billed for compute; disks and networking keep charging | Does not cover what the platform does on credit exhaustion |
| P-AZ-8 | [Public IP pricing](https://azure.microsoft.com/en-us/pricing/details/ip-addresses/) | none shown | Public IPv4 is billed per hour; no charge for dynamic IPs on stopped-deallocated VMs | Prices did not render in the fetched copy. No student free allowance is stated |
| P-AZ-9 | [Azure free services](https://azure.microsoft.com/en-us/pricing/free-services/) | none shown | 12-month free amounts for new customers | The fetched copy did not list quantities. Rely on the account's Free services blade (E-AZ-08) instead |
| P-OC-1 | [OCI Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm) | none shown | Free Trial ($300, up to 30 days) plus Always Free; a card is usually required and "will not be charged unless you upgrade"; Always Free continues after the trial; paid resources are reclaimed | No grace period for reclaimed resources; nothing on acceptable use |
| P-OC-2 | [OCI Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) | none shown (© 2026) | Home region only; E2.1.Micro ×2; A1 at 1,500 OCPU-h and 9,000 GB-h per month; 200 GB block storage; 10 TB/month outbound; idle reclamation thresholds; capacity may be unavailable | Boot volume size given as both 47 and 50 GB. The A1 quota differs from older copies (4 OCPU / 24 GB). Reclamation outcome (stop or delete) not stated |
| P-OC-3 | [Oracle Free Tier FAQ](https://www.oracle.com/cloud/free/faq/) | not read | — | Returned HTTP 403 to the automated fetch on Oct 28. Arnav should read it in a browser |

Community answers on Microsoft Q&A appeared in searches (for example, deployed products
being "decommissioned" after the subscription is disabled). They are **not** provider
policy and are not used as evidence here.

## 4. Decision table

Each criterion is `pass`, `fail` or `unknown`, with a reason. A candidate is acceptable
only if every criterion is `pass`. A single `fail` rejects it. Any `unknown` blocks
creating resources.

| Candidate | Eligibility | Permitted use | Allowance and resource fit | Payment and charge risk | Exhaustion behaviour | G-4 outage compatibility | Evidence owner | Evidence link / status | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Azure for Students** (first candidate, REQ-5) | **unknown**: needs E-AZ-01 to 04 (A) | **unknown**: the terms are silent on this use; needs E-AZ-05 (S) | **unknown**: needs E-AZ-07 to 10 and 15. The SKU must be listed as free for this subscription, and the credit must outlast Nov 4 with margin | **unknown**: needs E-AZ-11 to 14 (no payment method, spending limit on, no Marketplace items, not upgraded) | **unknown**: P-AZ-5 says VMs are deallocated and IPs lost; disk retention and any charge are unconfirmed (E-AZ-17 to 19, S) | **unknown, blocked**: no G-4 bound exists. Deallocation on exhaustion is an outage with orders possibly left resting | Arnav | Not started (Oct 28) | **Pending.** Do not create resources |
| **Oracle Cloud Always Free** (conditional, OQ-H4) | **unknown**: needs E-OC-01 | **unknown**: needs E-OC-02 (S) | **unknown**: needs E-OC-04; capacity is not guaranteed | **unknown**: a card is attached by design; passes only with E-OC-03 (A and S) | **unknown, at risk**: idle reclamation (E-OC-06) may hit a low-load engine | **unknown, blocked**: as for Azure, plus reclamation as an extra outage cause | Arnav | Not evaluated: only after a recorded Azure `fail` | **Not evaluated** |

**Reject a candidate if any of these is true:**

1. Arnav is not eligible, or the offer is not active (E-AZ-01, E-AZ-04, E-OC-01).
2. The provider says the use is not permitted, or will not confirm it in writing when the
   terms are ambiguous.
3. A payment method could be charged for any resource in the design, the spending limit
   is off or can be removed without Arnav knowing, or the account is upgraded or would
   need upgrading (E-AZ-11 to 14, E-OC-03).
4. A resource in the design is billed at all and the provider will not bound it, or
   projected credit use through Nov 5 exceeds the remaining credit with margin (E-AZ-10).
5. The credit or the free allowance ends before the trading window closes (E-AZ-03).
6. Exhaustion or reclamation would delete the disk holding the idempotency journal
   without notice. It cannot be accepted until the design keeps exposure within the
   G-4 bounds during the resulting outage (G-9's second clause).
7. The proposed SKU, disk or region is not available under the quotas and policies of
   this account (E-AZ-06, E-AZ-15, E-OC-04).

**If both candidates are rejected, or neither can be decided:** H1 hosting stays
**blocked**, G-9 stays not met, and the system stays in dry run without a server. Under
the PRD that is an acceptable outcome; paid hosting is out of scope (REQ-4).

## 5. Unresolved items

- **G-9: not met, pending Arnav's evidence.** All rows in section 4 are `unknown`.
- **G-4 has no evidence**, so G-9's second clause cannot be assessed. That clause is "the
  deployment plan keeps exposure within the G-4 bounds when the allowance ends". See
  [`design.md` section 9](design.md#9-outage-protection-req-14-and-g-4-dependencies).
- **Permitted use** needs written provider confirmation for either candidate. No public
  page settles it.
- **Data and disk retention after an Azure for Students subscription is disabled** is not
  documented in any official page found.
- **Whether credit exhaustion gives the guest OS a shutdown signal** is undocumented. The
  design assumes it does not.
- **Timing.** The trading window closes Nov 4, 10:30pm IST, seven days after this
  package was written. H2 also depends on M3, S1 and C1, none of which exist yet. Arnav
  may decide that collecting account evidence now is not worth it. If so, record that
  decision here.
