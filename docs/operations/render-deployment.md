# Render production deployment

This guide deploys one Django web service, one Render PostgreSQL 18 database,
one private persistent disk for approved employee evidence, and one low-cost
outbox cron job. It is sized for the first 5–8 active users and intentionally
does not add Kubernetes, Redis, replicas, or autoscaling.

## Before creating anything

1. Commit this deployment bundle to a private GitHub or GitLab repository.
2. Confirm that all business data is backed up locally. Do not seed or import
   demo data into the production database.
3. Resolve the production access-control launch gate: restrict privileged
   Django admin access and verify each role against the production database.
4. Review every attachment workflow. The disk in `render.yaml` is persistent
   for a single web instance, but it is not a substitute for an off-platform
   encrypted backup. Migrate sensitive HR files to private object storage
   before permitting routine uploads.

## Create the services

1. Sign in to Render and connect the private source repository.
2. Select **New > Blueprint**, choose the repository, and allow Render to read
   `render.yaml`.
3. Confirm the shown resources before applying them:
   - `jadwa-rms-web` on the paid Starter web plan;
   - `khan-mandi-rms-db` on the paid Basic 1 GB PostgreSQL plan;
   - `khan-mandi-rms-outbox`, scheduled every ten minutes.
4. Apply the Blueprint. Render generates the Django secret key, provisions the
   database, builds the Docker image, runs migrations before traffic reaches
   the new version, and checks `/healthz/`.
5. Open the generated `onrender.com` address and verify that the sign-in page
   and static CSS load correctly.

## Add the real domain

1. Add the domain in the web service's **Custom Domains** section and update
   its DNS records exactly as Render displays.
2. Add the domain (without `https://`) to `DJANGO_ALLOWED_HOSTS` in the web
   service and cron service, separated by commas. Keep the `onrender.com`
   hostname until acceptance testing is complete.
3. Deploy once more, sign in using an owner account, and test a complete,
   non-financial workflow before allowing staff access.

## Post-launch controls

- Enable paid PostgreSQL point-in-time recovery and keep daily logical exports
  in a separate encrypted storage account.
- Once per quarter, restore a backup into a non-production database and record
  the result. A backup that has not been restored is not verified.
- Create Render notification alerts for failed deploys, health-check failures,
  cron failures, database storage growth, and high memory use.
- Keep production access limited to owners/managers; staff should use the
  application, never the Render dashboard or database credentials.
- Do not expose the database publicly. `ipAllowList: []` blocks public database
  access; use Render's private connection string from application services only.

## Rollback

If a release fails the health check or a key workflow fails after deployment,
use Render's **Rollback** action for the web service, then investigate against
a copy of production data. Do not run destructive migrations without a tested
backup and a written reversal plan.
