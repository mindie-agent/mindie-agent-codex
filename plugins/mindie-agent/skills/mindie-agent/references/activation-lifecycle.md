# Capture and operation lifecycle

The installation's saved sharing choice and project scope govern capture.
Normal use needs no activation command, status check or closing action.
The Stop event identifies one current native task. Its named transcript's
profile location, owner, project and task/fork creation boundary are checked
before the adapter associates it internally. No directory or history scan runs.
Association time is not an authorization boundary: the first authorized turn
is retained. Explicit disable stays disabled, and re-enable never backfills the
off period. Explicitly revoked task associations are not recreated automatically.

Knowledge reads are independent of capture. Feedback uses verified host task
identity and the observed reference; it stays local without an eligible capture
scope. The model supplies neither task identity nor an activation token.

An operation has no default execution deadline. Process exit, closed protocol
pipes, connection errors, owner exit and explicit cancellation are observed;
quiet or long-running execution is not an error. User-requested deadlines are
enforced by the actual executor. Known terminal results survive cleanup faults.
Unknown external writes or model outcomes retain their receipts and are never
blindly replayed. Saved model output resumes local processing without a new call.

Normal and failed Hooks return a neutral response and no user instructions.
Existing local diagnostic records preserve faults. A later natural capability
call carries bounded machine diagnostic references for the Agent; delivery is
acknowledged only after writing that response. No later call means no delivery,
not successful repair. These internal faults do not create a user maintenance
step or change an unrelated business result.

Updates wait for actual in-flight calls and maintenance, then switch the selected
generation. Active execution retains its generation until ownership is released.
Remote durable jobs keep their existing identity and independent lifecycle.
