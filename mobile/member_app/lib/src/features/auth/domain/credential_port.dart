/// The two-factor sign in the backend is being built to: member number and
/// PIN, then an OTP to the registered phone or email.
///
/// # This is a PROPOSAL
///
/// None of these endpoints exist. `develop` carries OTP-only sign in and no
/// PIN at all. This port is written so the screens can be built and reviewed
/// now, and so the server half has something concrete to agree with or
/// correct. `docs/technical/member-auth-contract-proposal.md` states the same
/// shapes in one place, with the open questions.
///
/// # The one thing that must not be got wrong
///
/// [signIn] must answer IDENTICALLY for a wrong PIN and for a member number
/// that does not exist. Member numbers are sequential and guessable in a way
/// email addresses are not, so a login screen that distinguishes the two
/// becomes a membership enumerator: walk the numbers, read which ones say
/// "wrong PIN", and you have the register. That is a worse oracle than the
/// one gate 1.6 already prevents on the OTP route, and it is invisible unless
/// it is designed for from the start.
///
/// The consequence for this port: a failed [signIn] is one [ApiError] with
/// one meaning, and the client cannot tell the two apart because it is never
/// told.
library;

import 'package:gp_api_client/gp_api_client.dart';
import 'package:meta/meta.dart';

import '../../../core/session.dart';
import 'member_credential.dart';

/// Where a dispatched code went, and until when.
///
/// Returned by [CredentialPort.signIn] — that is, only AFTER the PIN has been
/// verified. That ordering is what makes the masked destination safe to show:
/// the member has already proven who they are, so telling them where their
/// own code went reveals nothing they did not already know.
///
/// In today's OTP-only flow nothing is proven before the code is sent, and
/// the screen has to hedge with "if that account is registered". The hedge
/// disappears here, and it disappears for a reason rather than because the
/// copy got bolder.
@immutable
class OtpChallenge {
  const OtpChallenge({
    required this.id,
    required this.destination,
    required this.expiresAt,
  });

  /// Opaque handle for the dispatched code. Sent back with the code so the
  /// member number and PIN never travel twice.
  final String id;

  /// Masked, and masked by the SERVER. A client that masked a full number it
  /// had been sent would be a client that had been sent a full number, which
  /// is the thing worth avoiding. Example: `07XX XXX 678`.
  final String destination;

  /// When the code stops being accepted.
  ///
  /// An absolute moment, not a duration, so a countdown cannot drift with a
  /// slow round trip. The server holds `OTP_TTL_SECONDS = 300` today and does
  /// not return it; the ask is that it does, because the alternative is this
  /// app hardcoding a server constant and showing the wrong number from the
  /// day somebody tunes it.
  final DateTime expiresAt;
}

/// Sign in, and the PIN lifecycle around it.
abstract interface class CredentialPort {
  /// Step one: verify the PIN and dispatch a code.
  ///
  /// Throws [ApiError]. A wrong PIN, an unknown member number and a locked
  /// account must be indistinguishable to this method — see the library doc.
  /// Lockout, when it engages, is the ONE case that may be distinguishable,
  /// because a member who cannot get in needs to be told to call the SACCO
  /// rather than left retrying a PIN that will never work.
  Future<OtpChallenge> signIn(MemberNumber number, MemberPin pin);

  /// Step two: exchange the code for a session.
  Future<TokenPair> verify(OtpChallenge challenge, String code);

  /// Send another code for the same challenge.
  ///
  /// Returns a fresh [OtpChallenge] because the expiry moves, and a screen
  /// showing a countdown against the old one would be counting down to the
  /// wrong moment.
  Future<OtpChallenge> resend(OtpChallenge challenge);

  /// Forgot PIN, step one: dispatch a code without a PIN.
  ///
  /// This one CANNOT verify anything first, so it is back in the same
  /// position as today's OTP route: it must answer identically whether or not
  /// the member number exists, and the screen must hedge accordingly. The
  /// masked destination therefore must NOT be returned here — returning it
  /// would confirm the number is real.
  Future<OtpChallenge> requestPinReset(MemberNumber number);

  /// Forgot PIN, step two: prove the code and set a new PIN in one call.
  ///
  /// One call rather than two on purpose. Splitting it would leave a window
  /// where a verified code is spendable on its own, and that window is a
  /// credential.
  Future<void> resetPin(OtpChallenge challenge, String code, MemberPin pin);

  /// A member signing in for the first time, who has no PIN yet.
  ///
  /// Reachable only from a challenge the server has marked as needing one —
  /// the client never decides that a member is new.
  Future<TokenPair> setInitialPin(
    OtpChallenge challenge,
    String code,
    MemberPin pin,
  );
}
