# Ephemeral AI Code Interpreter: System Requirements

Source: `Ephemeral Confidential AI Code Interpreter Architecture.docx`.

These requirements extract the intended system behavior and security outcomes
without prescribing ACI, Container Apps, Foundry, IIS, or a specific hardware
technology. They are a baseline for assessing whether Container Apps can satisfy
the requirements, not a claim that it already does.

## Confidentiality and trust

1. **Administrator confidentiality:** Azure administrative privileges alone must
   not allow someone to obtain session files, code, results, or secrets, including
   while execution is running.
2. **Explicit plaintext boundary:** Only the trusted calling application and
   the approved execution environment may handle sensitive plaintext. The
   interpreter is a tool used by on-premises agents; the calling application
   is not required to run on-premises.
3. **Tamper resistance:** Administrator changes to the execution environment must
   either preserve confidentiality or cause the application to reject the
   environment before releasing sensitive information.
4. **Verified execution identity:** Before releasing secrets or data, the
   application must independently establish that it is communicating with the
   approved execution environment and configuration. Evidence must be fresh,
   replay-resistant, and bound to that session's cryptographic identity.
5. **Accept availability attacks:** Administrators may be able to stop, delete,
   or disrupt sessions. Those actions must cause failure, not disclosure.

## Identity, authorization, and communication

6. **User authorization:** Authenticate and authorize the requesting user before
   creating or accessing a session.
7. **Least-privilege application identity:** Give the calling application only
   the Azure permissions needed to provision, operate, and destroy the execution
   resources.
8. **Restricted access:** The interpreter must not be directly exposed to the
   public internet. Restrict inbound access to approved systems. Private network
   connectivity from on-premises is not required.
9. **Protected communications:** Encrypt and authenticate sensitive traffic end
   to end between the trusted application and approved execution environment.
   Network location alone must not establish trust.
10. **Session-specific authorization:** Every sensitive interpreter operation must
    require session-specific authentication. Network reachability or Azure
    permissions alone must not grant access to session contents.

## Session secrets and lifecycle

11. **Unique ephemeral secrets:** Generate a new cryptographically random secret
    for every session. The document specifies a 256-bit symmetric key.
12. **Restricted secret residency:** Usable session secrets must exist only in
    trusted application memory and the approved execution environment, not in
    configuration, environment variables, databases, persistent storage, or logs.
13. **Session isolation:** Each interpreter session must have its own isolated
    execution environment. Sessions must not access one another's data or
    credentials.
14. **No recovery or resumption:** Application restart, execution-environment
    restart, failed verification, or significant communication failure must
    invalidate the session. A retry must use a new environment and new secrets.
15. **Deterministic termination:** Destroy the session environment and discard
    its secrets on completion or failure.
16. **Independent orphan cleanup:** Enforce a short maximum lifetime and remove
    abandoned environments using resource metadata only, without requiring
    session secrets or contents.

## Code execution and data handling

17. **Interpreter functionality:** Support file upload, listing, retrieval,
    deletion, Python execution, execution status, stdout/stderr, generated-file
    discovery, and health checks.
18. **Separate untrusted-code:** Isolate AI-generated Python from the
    trusted interpreter service, its memory, cryptographic keys, and credentials.
19. **Resource limits:** Enforce execution timeout and limits on CPU, memory,
    processes, and filesystem consumption.
20. **Restricted outbound access:** Prevent arbitrary internet access, package
    installation, access to unrelated corporate systems, and access to cloud
    metadata or management credentials. Allow only explicitly required
    dependencies.
21. **Protected temporary data:** Avoid persistent plaintext storage. Establish
    where temporary files reside, how they are protected while present, and what
    remains after destruction.
22. **Direct result delivery:** Return sensitive results through the protected
    session channel rather than through ordinary logs or persistent intermediary
    storage.

## Logging, deployment, and assurance

23. **Content-free operational telemetry:** Record lifecycle events, verification
    outcomes, durations, transfer sizes, and error categories, but not secrets,
    sensitive code, file contents, sensitive filenames, stdout/stderr, or
    plaintext request/response bodies.
24. **Approved, immutable deployment:** Build and approve a known interpreter
    image/configuration, identify it immutably, and prevent an unapproved
    replacement from receiving session secrets.
25. **Demonstrable enforcement:** Prove these controls through a working
    end-to-end prototype and negative tests covering unauthorized access,
    modified environments, replay, restarts, interruption, sandbox escape paths,
    and cleanup.

## Open acceptance decision

Requirements 1-4 must be clarified before selecting an implementation:

- Must protection hold against all Azure administrators, including those able to
  change the governing policies and permissions?
- Or must protection hold against workload administrators constrained by
  separately controlled policies and permissions?

The source document describes the stronger outcome: Azure administrative control
alone must not yield session plaintext. Accepting the narrower administrative
boundary would be an explicit requirements decision, not an implementation detail.

## Key establishment (applies to requirements 9, 11 and 12)

The requirements say each session needs a fresh secret that is never sent in the
clear and never stored. The approach is to use **ECDH key agreement** (HPKE is
the standardized form of the same idea):

1. The sandbox generates its own key pair. The private half never leaves it.
2. The orchestrator generates a one-time (ephemeral) key pair for the session.
3. They exchange only public keys. Each side combines its private key with the
   other's public key (ECDH, X25519) and runs the result through HKDF-SHA256,
   bound to the session id. Both get the same 256-bit session key.
4. The session key is never transmitted, so nobody watching the traffic, or
   reading a file or log, can obtain it. It lives only in orchestrator memory
   and the sandbox, and is discarded when the sandbox is deleted.
5. The session key encrypts data (AES-256-GCM) and authenticates every
   operation (HMAC).

This meets: 9 (end-to-end encryption without trusting the network), 11 (a new
random key every session), and 12 (the key is derived, never put in config,
environment variables, storage or logs).

What it does not prove: that the sandbox is really the expected code. Without
hardware attestation, the orchestrator pins the approved image id and checks
the interpreter files before releasing data (requirements 3 and 4).

## Technology choices versus requirements

ACI, a particular hardware technology, IIS specifically, and a particular
attestation service are not prescribed by this technology-neutral baseline.
Any alternative must still demonstrate the required verification and
confidentiality outcomes.

The document's choices to avoid Key Vault and a separate session broker simplify
the proposed architecture. They are not the underlying confidentiality outcome.

## Proposed implementation

The ACA Sandboxes design, per-requirement fit, and validation tests are in
`sandbox-approach.md`.
