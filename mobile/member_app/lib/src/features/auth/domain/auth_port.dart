/// What the auth feature needs from the network, stated without naming it.
///
/// Layering (P17 §3.1): `presentation -> domain <- data -> gp_api_client`.
/// The port lives in `domain/`, the implementation in `data/`, and the screens
/// see only this. That is what lets the controller's tests run against a fake
/// with no HTTP anywhere near them.
///
/// One deliberate exception to "domain names no transport type": failures
/// surface as [ApiError]. It is tempting to re-wrap it in a domain-flavoured
/// failure enum, and that would be re-implementation — gate 1's rejected MR.
/// [ApiError] is already the sanitized, category-opaque envelope the whole
/// system speaks, carrying no HTTP semantics beyond a status code and no
/// server prose at all. Re-wrapping it would add a mapping to maintain and
/// subtract nothing. What the domain must NOT do is let that envelope reach a
/// widget, and it does not: the controller turns it into member-facing state.
library;

import 'package:gp_api_client/gp_api_client.dart';

import '../../../core/session.dart';
import 'sign_in_identifier.dart';

/// `POST /member/auth/*`, as the auth feature needs it.
///
/// Every method throws [ApiError] and nothing else — the transport converts
/// socket failures, pin rejections and non-2xx responses into it, so a caller
/// has exactly one failure type to handle.
abstract interface class AuthPort {
  /// `POST /member/auth/otp/request` — 202, `{"status":"sent"}`, always.
  ///
  /// Returns nothing because there is nothing to return. The server answers
  /// identically whether or not the credential exists (gate 1.6: no existence
  /// oracle), so any value here would be an invitation for a screen to branch
  /// on something that carries no information.
  ///
  /// [freshIntent] rotates the Idempotency-Key. False for a retry of a send
  /// the member already asked for; true for an explicit "Resend" — see
  /// `data/member_auth_repository.dart`, where it matters more than it looks.
  Future<void> requestOtp(SignInIdentifier identifier,
      {bool freshIntent = false});

  /// `POST /member/auth/otp/verify` — the member-audience token pair.
  ///
  /// Throws [ApiError] with [ApiFailureKind.unauthenticated] for a wrong or
  /// expired code. That 401 does NOT mean "the session ended": there is no
  /// session yet. Callers must not treat it as one.
  Future<TokenPair> verifyOtp(SignInIdentifier identifier, String code);

  /// `POST /member/auth/refresh` — rotates the family.
  Future<TokenPair> refresh(String refreshToken);
}
