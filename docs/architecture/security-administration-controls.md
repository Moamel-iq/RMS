# Security administration controls

## Boundary

ERP security administration is an organization-scoped business act.  It is
not granted by Django's `is_staff` flag, which only controls access to the
separately locked-down Django admin site.

The `organizations.Organization` permission vocabulary is:

- `manage_users`
- `manage_access`
- `manage_roles`
- `view_audit`
- `manage_org_settings`

Each request must hold the permission through an active
`OrganizationMembership` in the organization being changed.  A branch role
does not assemble into organization-wide security authority.

## Built-in role defaults

`OWNER` receives all five security permissions.  `ACCOUNTING_MANAGER` receives
only `view_audit`; all other built-in and custom roles receive none.  Custom
roles cannot be configured with `organizations.*` permissions.

## Maker-checker for ownership and access

- The ERP account forms never expose `is_staff` or `is_superuser`.
- New ERP accounts are created as non-staff and receive only an organization
  `VIEWER` membership.  A separate access action is needed for an operational
  role.
- UI access changes create `AccessChangeRequest`, never a membership directly.
  It records the organization, optional branch, target, proposed role, prior
  access snapshot, reason, requester, reviewer, and decision time.
- The requester and the target may not approve or reject their own request.
  A request is row-locked during the decision, so two reviewers cannot apply
  it twice. Rejections and cancellations require a stored reason.
- `OWNER` is allowed only at organization scope; an owner request still needs
  another authorized user to approve it.
- Staff and superuser accounts cannot be targets. Direct grant/revoke helpers
  reject an HTTP actor; their actor-less form is retained only for fixtures,
  bootstrap, and controlled data migrations.
- Both the request decision and the resulting membership mutation are written
  to the append-only audit trail under the organization scope.

## Django Admin

`/admin/` is a break-glass surface and accepts active superusers only. An
ordinary `is_staff` account receives a 403 after login. The navigation does not
offer an Admin link, and both branch and organization membership records are
read-only in Admin so it cannot bypass maker-checker.

## Audit visibility

`AuditEvent.organization` is now an explicit tenant boundary. New events infer
it from the passed branch or target relation when it is not supplied. Migration
`core.0005` backfills historical events with a branch using that branch's
organization while the append-only update trigger is disabled for that one
transaction only. Events with no provable scope remain hidden from
non-superusers rather than risk cross-tenant disclosure.

## HR document review

Employee document metadata and downloads require `hr.view_employee_personal`,
not merely the general employee-workspace permission. Attachments are served
as downloads through a scoped view, each download creates a
`DOCUMENT_DOWNLOADED` audit event, and the raw `media/hr/employee-documents/`
route is blocked before Django's DEBUG media helper can serve it.

Production storage must still keep that prefix private at the web server or
object-storage layer; the Django route cannot protect a separate public CDN.

## Daily closing review

The cashier shift is the daily close control. Closing and approving are
separate actors; the service checks the separation under a row lock and the
database constraint `sales_shift_approver_is_not_the_closer` enforces it even
outside the service. The freeze trigger prevents changes to counted/expected
figures once closed or approved, and any approved shift reversal needs a
reason and audit event. The daily reconciliation screen is intentionally
read-only and recomputes from the protected source documents; it has no
"acknowledge" button that could hide a real variance.

## Deployment

Apply migrations through `core.0006_audit_action_document_downloaded` and
`organizations.0007_accesschangerequest`. The post-migrate hook synchronizes
the security permissions into built-in role groups. Existing users need an
explicit `OrganizationMembership` with the appropriate role; an existing
`is_staff=True` flag alone intentionally confers no ERP security access.
