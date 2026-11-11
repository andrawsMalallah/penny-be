# Device verification feasibility and proposed proof

Assessment date: 2026-11-09. Status: deferred research, not an approved server protocol.
An isolated native diagnostic and manual mobile-build configuration are prepared in the
separate mobile workspace. Real-device attestation and server verification are unproven;
other-device revocation remains disabled. Backend financial implementation was explicitly
approved while this verification work remains pending; the requirement itself is unchanged.

## Requirement and conclusion

Penny requires biometric or phone-lock verification before revoking another session,
alongside server ownership authorization. A client success flag cannot authorize this.
Initial login stays username/password plus optional TOTP; local unlocking stays separate.

Recommendation: evaluate an Android Keystore key whose use requires authentication,
with server-validated attestation. This is a candidate, not a selected production protocol.
The existing Python dependency set has not been shown to provide the complete verification
path. Do not implement a custom certificate/attestation parser or invent a token protocol.
The native/server proof must establish a maintained, reviewed integration before endpoints.

Support must remain generic across supported Android manufacturers/models. A particular
phone supplies test evidence only; its brand proves no biometric or attestation capability.
Minimum application support is Android 7/API 24. A future proof must record actual model,
Android version, strong-biometric availability and hardware/key policy at runtime.

## Options evaluated

| Approach | Assessment for Penny |
| --- | --- |
| Local authentication callback sent to Django | Does not cryptographically establish verification; reject as server proof. |
| Credential Manager with WebAuthn user verification | Standard and useful for account verification, but synced/cross-device credentials do not establish the current phone. Not selected for this requirement. |
| Authentication-bound Android Keystore key with verified attestation | Closest candidate: ties proof to an enrolled app key and its enforced policy. Needs native fallback tests and maintained server integration. |
| Local unlock plus fresh server password/TOTP | Simpler possible product alternative, but changes the agreed server-proof requirement. Not adopted. |

Passkeys can synchronize and support authentication from another phone; requiring WebAuthn
user verification does not by itself change that. See [Google's supported-environments
guide](https://developers.google.com/identity/passkeys/supported-environments).

## Platform findings and limits

Android supports authentication-bound keys and cryptographic operations through
BiometricPrompt.CryptoObject. Auth-per-use and time-window keys have different guarantees.
An earlier screen unlock or a successful callback alone must not be accepted as approval
of a new revocation. See [CryptoObject](https://developer.android.com/reference/android/hardware/biometrics/BiometricPrompt.CryptoObject)
and [key policy](https://developer.android.com/reference/android/security/keystore/KeyGenParameterSpec.Builder#setUserAuthenticationParameters(int,%20int)).

The biometric guide documents Android-version restrictions on combined biometric/credential
prompts and a different flow for time-window keys. Start the proof on Android 11/API 30 or
newer as a proposed baseline, not an approved minimum app version. Test biometric and
PIN/password/pattern paths separately; do not assume the same CryptoObject configuration
works on every device. See [Android's biometric guide](https://developer.android.com/identity/sign-in/biometric-auth).

Key attestation is verified off-device. Trust requires more than checking a signature:
certificate roots, revocation and relevant attested properties matter. Google's recommended
verifier is Kotlin. Trust material has an update lifecycle. See [Android's attestation
guide](https://developer.android.com/privacy-and-security/security-key-attestation).

Attestation and a valid signature can establish use of the enrolled key under a verified
policy. They do not establish physical co-location of an arbitrary HTTP client or eliminate
relay attacks. Enrollment/session binding and the threat model still need review; avoid
claiming that the server can identify a physical phone from its display name or a UUID.

## Library and infrastructure candidates

| Candidate | Finding and approval boundary |
| --- | --- |
| Android framework Keystore/BiometricPrompt; Jetpack androidx.biometric | Native API candidates. Select and pin the Android library only after build/API compatibility review. Flutter alone does not prove native key-policy behavior. |
| android/keyattestation | Official Kotlin server verifier candidate. It adds JVM/build/runtime considerations to a Python application; no helper, service or architecture change is approved. Policy checks must accompany chain verification. |
| Yubico python-fido2 | Maintained WebAuthn server candidate if the requirement changes or a suitable authenticator path is demonstrated. Previously removed trial package; do not reinstall implicitly. |
| Duo py_webauthn, PyPI name webauthn | Python WebAuthn candidate with android-key format support. Format support does not establish compatible native credential creation or full key-policy/trust validation. Not approved or proven on Python 3.14 here. |
| Existing cryptography | Supplies cryptographic primitives, not evidence of a complete Android attestation-policy verifier. No home-grown replacement. |

Sources: [official Kotlin verifier](https://github.com/android/keyattestation),
[Yubico server API](https://developers.yubico.com/python-fido2/API_Documentation/autoapi/fido2/server/index.html),
[Duo documentation](https://duo-labs.github.io/py_webauthn/),
[Duo android-key change history](https://github.com/duo-labs/py_webauthn/blob/master/CHANGELOG.md).
No versions were selected, installed or audited in this assessment.

WebAuthn would also require an RP domain, HTTPS, exact allowed origins, an Android package
identity and signing-certificate association through Digital Asset Links. These are not
configured. Direct Keystore attestation has different app/signing checks; it does not gain
WebAuthn origin guarantees automatically. See [Credential Manager prerequisites](https://developer.android.com/identity/credential-manager/prerequisites).

Keep Django/PostgreSQL and hosting portable. Android trust roots/revocation are platform
security dependencies, not hosting choices; their operational cost still needs approval.
Do not add Play Integrity, a cloud attestation service or Play Store distribution implicitly.

## Proposed operation lifecycle, independent of wire-format choice

These are acceptance rules, not new API routes, token formats or database authorization.

1. Enroll only after full sign-in, fresh password and MFA when enabled. Verify a fresh
   enrollment challenge, app/signing identity, key ownership, hardware/security policy and
   trust chain before binding the public key to the owned device session. Re-enrollment
   requires the same checks; possession of a stolen session cookie alone is insufficient.
2. For revoke-one, bind a fresh server challenge to the account, requesting device/session,
   enrolled key, exact target and action. Proposed expiry: two minutes. Revoke-all-others
   has a distinct action, excludes the requesting device and defines its target scope at
   execution. Show this scope before authentication.
3. Native authentication must enable the approved cryptographic operation. Cancellation,
   backgrounding, missing screen lock, invalidated key and unsupported authentication fail
   without performing revocation. Never persist or transmit the phone credential itself.
4. Verify proof with the approved maintained protocol implementation. Under the existing
   user-first lock order, recheck requesting session/key validity, ownership, expiry and
   unused challenge state; consume proof and revoke target server sessions atomically.
   No reusable 'verified recently' grant. Failed attempts consume a bounded rate budget.
5. Reject changed targets/actions, cross-account/session use and replay. Concurrent proof
   submissions have one winner. Invalidate pending challenges on relevant credential or
   session changes. Session rotation must not leave grants bound to an obsolete identifier.

The eventual design must settle wire format, trust-policy validation, enrollment renewal,
revocation freshness/cache failures and key lifecycle before implementing this lifecycle.
Do not substitute an ad hoc signed blob for a maintained authentication protocol.

## Smallest proposed implementation sequence

1. **Collect native proof using the prepared diagnostic:** exact test phone/version; stable
   test app identity/signing; Keystore authentication and fallback matrix. Diagnostic and
   manual mobile-build preparation are approved, but publication, hosted execution, signing
   setup and actual phone checks retain their own approval boundaries. No real revoke API.
2. **Review verifier integration:** select an exact maintained wire protocol/library and
   prove attestation/policy validation using synthetic and real-device evidence. Evaluate
   JVM cost explicitly if using the official verifier. Stop if this requires custom crypto
   or a homemade authentication format; bring the concrete choice back to the user.
3. **Approve backend implementation:** migrations, bound challenges and atomic revoke-one/
   revoke-all-others, with API/ownership/PostgreSQL race tests. Require successful native
   proof before enabling the feature; mocks cannot satisfy that gate.

A portable Flutter SDK and a Flutter application now exist in the separate mobile
workspace, alongside the native diagnostic. No emulator is installed. Browser previews
cannot exercise Keystore/phone credentials. An installed APK and device access are needed
for native proof; end-to-end server proof additionally needs approved verifier integration
and a secured test backend. Local unlock/transport source does not establish server proof.

## Real-phone and server acceptance checklist

- Strong biometric success, failure, cancellation, lockout and credential fallback.
- Credential-only phone; no screen lock; unsupported hardware/Android version.
- A new action always requires the intended fresh authentication; ordinary screen unlock
  and a forged callback do not authorize key use or server revocation.
- Background/reopen, restart, key invalidation, biometric enrollment changes, reinstall,
  backup/restore and signing-key changes; no silent downgrade or copied private key.
- Reject invalid/revoked trust chains, wrong app signer/challenge/key/policy and stale trust
  data according to the approved outage policy. No phone identifiers or secrets in logs.
- Wrong user/session/target/action, expiry boundary and concurrent/repeated submissions.
- Revoke only the intended sessions; keep the current device; revoked cookies lose access.
  Concurrent sign-in, password/MFA change and failed transaction rollback preserve invariants.

## Evidence and resume point

This assessment supplied no server implementation, selected wire protocol or approved
verifier. Native diagnostic/manual mobile-build preparation and Flutter local-security
source exist, but installed APK/device/attestation evidence remains pending. Read
[the backend README](../README.md) for current verification boundaries. Mobile work is
paused until backend rollout; this research does not resume the old probe/build sequence
automatically. When that milestone resumes, collect generic Android evidence and approve
a maintained verifier integration before implementing server-verification endpoints.
