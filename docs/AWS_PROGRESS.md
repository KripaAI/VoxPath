# VoxPath on AWS: progress log

> Personal working notes. Kept out of git on purpose (listed in `.git/info/exclude`)
> because it contains account details. The step-by-step plan is
> [`AWS_DEPLOYMENT_ROADMAP.pdf`](AWS_DEPLOYMENT_ROADMAP.pdf).

**Last updated:** 5 October 2026
**Where I am:** Stage 0 (Protect your wallet), about half done
**Next step:** finish the Stage 0 items under *Up next* below, starting with the EC2 credit task

---

## Goal

Host VoxPath on AWS with a full CI/CD pipeline, so I can show I'm an
end-to-end agentic AI developer: build, ship, automate, measure (evals) and
operate an AI agent. Learning project, so **keep costs minimal** ("learning mode").

## Account facts

| Item | Value |
|---|---|
| AWS account ID | 287238357482 |
| Region | `ap-south-1` (Mumbai) for everything |
| Plan | **Free plan**, active. Ends **30 March 2027** (or when credit runs out) |
| Signed up | ~30 September 2026 |
| Credit | **$120** (started at $100; +$20 from the budget task) |
| Root user | MFA on ✅, no access keys ✅. Use only for emergencies |
| Admin user | `kripa-admin`: `AdministratorAccess` attached, no permissions boundary, MFA = passkey on this laptop |
| IAM access to billing | Activated (needed for Budgets/Credits as kripa-admin) |
| CLI profile | `voxpath` (region ap-south-1). **Still signed in as root, so switch it** (see Up next) |
| Old CLI keys | `[default]` in `C:\Users\krish\.aws\credentials` is expired. Delete once nothing needs it |
| Budget | `voxpath-monthly`, $25/month, alert at 80% actual only |
| DuckDNS | `voxpath-app.duckdns.org` and `voxpath-api.duckdns.org`. Both currently point at my home IP; repoint to the server in Stage 3. Keep the DuckDNS token private |

## Free-plan credit tasks ($20 each, up to $200 total)

| Task | Status |
|---|---|
| Set up a cost budget using AWS Budgets | ✅ Done (+$20) |
| Launch an instance using EC2 | ⬜ Not started (next) |
| Create a web app using AWS Lambda | ⬜ Not started |
| Create an Aurora or RDS database | ⬜ Not started. Delete the DB the same day |
| Use a foundation model in the Amazon Bedrock playground | ⛔ **Blocked**, see below |

### Bedrock is blocked on this account

Every Bedrock call fails with `ValidationException: Operation not allowed`, for
every model (Nova Micro, gpt-oss) in both `ap-south-1` and `us-east-1`, by plain
model ID and by inference profile. AWS does this on some brand-new accounts
until they have billing and usage history. It isn't a setup mistake.

Read-only checks (5 Oct 2026): Nova, gpt-oss and Llama show *region available,
entitlement available, agreement available*. Anthropic Claude's agreement is
**not available** on this account.

- [ ] Open a free **Account and billing** support case asking AWS to enable Bedrock
- [ ] Retry the playground (Nova Micro, Mumbai) in a week or two

**Impact:** none on the main plan. VoxPath uses my OpenAI key. Bedrock was only
for the optional model comparison (Stage 5f).

## Stage checklist

### Stage 0: Protect your wallet (in progress)
- [x] Root MFA on
- [x] IAM user `kripa-admin` with admin access + MFA
- [x] IAM access to billing activated
- [x] Budget `voxpath-monthly` created (earned $20)
- [ ] Add budget alerts: **100% forecasted** and **50% actual** (only 80% actual exists).
      Console: Budgets → voxpath-monthly → Edit → **Next** (not Save) → Configure alerts
- [ ] Switch the CLI to kripa-admin: `aws login --profile voxpath`, sign in as **kripa-admin** in the browser,
      then `aws sts get-caller-identity --profile voxpath` must end in `user/kripa-admin`
- [ ] Add a second MFA device to kripa-admin (phone authenticator) as a backup for the laptop passkey
- [ ] Delete the expired `[default]` keys from `~/.aws/credentials`
- [ ] Credit tasks: EC2 → Lambda → RDS (and Bedrock once unblocked)
- [ ] OpenAI: set a hard monthly limit (~$5)
- [ ] App: sign-up allowlist + rate limiting (Claude to write, with tests)

### Later stages (not started)
- [ ] Stage 1: Docker on laptop
- [ ] Stage 2: Images in ECR
- [ ] Stage 3: Server, DNS (DuckDNS), HTTPS
- [ ] Stage 4: CI/CD with GitHub Actions
- [ ] Stage 5: Prove the agent works (evals gate deploys, tracing, cost)
- [ ] Stage 6: Production basics (backups, alarms, runbook)
- [ ] Stage 7: Level up (optional): RDS, Terraform, Fargate, Jenkins/CodePipeline as short experiments

## Decisions made (and why)

- **CI/CD: GitHub Actions**, not Jenkins. The code is already on GitHub, CI already exists, there's no extra server to pay for, and OIDC means no stored AWS keys. Jenkins (on my laptop in Docker) or CodePipeline become optional Stage 7 experiments.
- **LLM: keep OpenAI** (`gpt-4o-mini`) for now; don't switch to Bedrock. It avoids code changes and keeps memory embeddings (1536-dim) valid. Bedrock is an optional comparison later.
- **Domain: free DuckDNS** subdomains instead of a paid domain.
- **One EC2 server (t3.small) with Docker Compose**, not ECS/Fargate. It's cheap, teaches the fundamentals, and the app is single-server today (news stored on local disk).
- **Learning mode** to minimise spend (roughly $4/month instead of $20–25):
  - run the server only during study sessions; **stop** it afterwards (don't terminate it); `sudo shutdown -h +180` as an auto-stop safety net
  - **no Elastic IP**: update DuckDNS on boot with `@reboot` cron (`...duckdns.org/update?domains=voxpath-app,voxpath-api&token=TOKEN&ip=`)
  - 15 GB disk; keep 3 images in ECR; 7-day S3 backup lifecycle; basic CloudWatch only
  - Stage 7 experiments: build and delete the same day
  - switch to always-on only when job-hunting, so the live link works
- **Never leave running:** NAT gateway, load balancers, RDS, unattached Elastic IPs, Bedrock Provisioned Throughput.

## Files

- `docs/AWS_DEPLOYMENT_ROADMAP.pdf`: the 20-page step-by-step roadmap (untracked)
- `docs/roadmap_src/`: the scripts that generate the PDF (`make_roadmap.py`), for future edits
- Not yet in the PDF: the **learning mode** section and its cost table (decided after the PDF was last built)

## Resume checklist (run these when I come back)

```powershell
aws sts get-caller-identity --profile voxpath          # who the CLI is signed in as
aws freetier get-account-plan-state --region us-east-1 --profile voxpath   # credit left, plan end date
aws freetier list-account-activities --region us-east-1 --profile voxpath --query "activities[].[title,status]" --output table
```

Then ask Claude: *"Read docs/AWS_PROGRESS.md and let's continue."*
