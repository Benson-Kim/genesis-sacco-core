# INSTITUTIONAL GAP REGISTER (v1.0)

Scope: what a deposit-taking institution runs that this system does
not. Distinct from `GAP_ANALYSIS.md` (prototype-coverage matrix) and
from the security-hardening issue tracker: those cover the system we
designed; this covers the institution we have not yet designed.

Standing assessment: the codebase is a well-engineered ledger kernel
at pilot stage — a safe custodian of test data, not yet of members'
money. The open hardening issues (#1–#7) are necessary and not
sufficient. Nothing in this register is closed by closing them.

Threat posture assumption: attacker capability equals or exceeds any
code review performed on this repo, permanently, at near-zero marginal
cost — AI-assisted vulnerability discovery is commodity. Design for
more bot attempts than human attempts on every public endpoint.

| # | Capability | Institutional norm | Status here |
|---|---|---|---|
| G1 | Channels | USSD (feature-phone reach), agency banking, PesaLink/RTGS, cards | Staff web only; member app blocked on ADR-0007; no USSD — members without smartphones are excluded entirely |
| G2 | Controls depth | Maker-checker on every financial mutation; amount-tiered approval limits as an engine; dual control on GL; branch scoping | Module×action grid; prototype's approval-authority bands (≤100k/≤500k/≤2M/Board) not implemented as a limits engine |
| G3 | External reconciliation | Automated EOD recon of ledger vs M-Pesa/bank statements; break management; unmatched-item aging | Does not exist in any form |
| G4 | AML/CFT | POCAMLA program, FRC STR filing, sanctions screening, cash-threshold reporting, periodic KYC refresh | Does not exist in any form |
| G5 | Fraud operations | Real-time rules engine, velocity/behavioral scoring, case management, member-facing alerts | Preventive controls only; zero detective controls (issue #1 is the first step, not the program) |
| G6 | Network & hosting | Dedicated/isolated infrastructure, WAF, DDoS absorption, network segmentation, secrets in KMS/HSM | Shared cPanel host: security boundary includes co-tenants; secrets in env vars; staging leaks plaintext OTP (being closed) |
| G7 | Security operations | SOC coverage, external penetration tests, vulnerability disclosure, incident response drills | None ever performed |
| G8 | Continuity | DR site, tested failover, RPO/RTO commitments, key ceremonies, BCP | Backup/restore work just started (agent session 6443005); everything else absent |
| G9 | Treasury & settlement | Liquidity management, interbank settlement, cheque clearing, GL of institutional depth | Fixed ≈20-account enum chart (ADR-worthy constraint, noted in hardening backlog) |
| G10 | People & process | On-call rotation, change advisory, regulator liaison, mandatory-leave/rotation for high-privilege staff | Not applicable yet as an org — recorded so it is never mistaken for a solved problem |

Rule for this register: entries are removed by shipping the capability,
never by re-describing it. Each entry graduates to a numbered issue (or
issue set) when work is scheduled; G2, G3, G4, G6 are opened as issues
alongside this commit.
