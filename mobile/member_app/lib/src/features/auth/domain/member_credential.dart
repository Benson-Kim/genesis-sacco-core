/// The two things a member types to start signing in: their number and their
/// PIN.
///
/// # These rules are PROPOSED, not confirmed
///
/// The endpoints behind them do not exist yet. `develop` carries OTP-only
/// sign in, where the identifier is an email address or a Kenyan mobile
/// number and there is no PIN anywhere. Everything here is written against
/// the flow the owner has described — member number and PIN, then an OTP to
/// the registered phone or email — and every rule is marked with what the
/// backend has to confirm.
///
/// `docs/technical/member-auth-contract-proposal.md` carries the same
/// questions in one place, for whoever builds the server half.
library;

import 'package:meta/meta.dart';

/// Why a typed member number cannot be sent.
enum MemberNumberProblem { empty, tooShort, tooLong, badCharacters }

/// A member number, in the form it will be sent.
///
/// **Format unconfirmed.** SACCO member numbers vary — some are plain
/// sequences, some carry a branch prefix, some a check digit. The rule here
/// is deliberately permissive: letters, digits, hyphens and slashes, trimmed,
/// 3 to 20 characters. It rejects what is obviously not a member number and
/// accepts everything plausible, which is the right bias while the real
/// format is unknown: a strict guess would lock out real members, and being
/// wrong in that direction is worse than one extra round trip.
///
/// It is deliberately NOT upper-cased or otherwise normalised. The server
/// owns normalisation, exactly as it owns msisdn normalisation today — one
/// normaliser, never a second one on the client that drifts from it.
@immutable
class MemberNumber {
  const MemberNumber._(this.value);

  /// Exactly what goes on the wire: trimmed, otherwise untouched.
  final String value;

  static final RegExp _shape = RegExp(r'^[A-Za-z0-9/-]+$');

  static const int _min = 3;
  static const int _max = 20;

  static ({MemberNumber? number, MemberNumberProblem? problem}) parse(
    String raw,
  ) {
    final String value = raw.trim();
    if (value.isEmpty) {
      return (number: null, problem: MemberNumberProblem.empty);
    }
    if (value.length < _min) {
      return (number: null, problem: MemberNumberProblem.tooShort);
    }
    if (value.length > _max) {
      return (number: null, problem: MemberNumberProblem.tooLong);
    }
    if (!_shape.hasMatch(value)) {
      return (number: null, problem: MemberNumberProblem.badCharacters);
    }
    return (number: MemberNumber._(value), problem: null);
  }
}

/// A PIN, held only long enough to send.
///
/// # Length is a security decision, not a layout one
///
/// The concept drawings show four digits. Four digits is ten thousand
/// combinations, and as the FIRST factor that is only safe behind
/// server-side attempt counting and lockout — without it, an attacker with a
/// member number walks the whole space. The OTP behind it limits the damage
/// but does not make the PIN itself sound, because a PIN that is known is a
/// PIN that survives every future session.
///
/// So [Flavor.pinLength] is a build-time constant rather than a hardcoded
/// four, the recommendation recorded for the owner is six, and the client
/// refuses trivially guessable values before they are ever sent. That last
/// part is not a substitute for server-side policy — a client check is
/// advice, since anything can post to the endpoint — but it stops a member
/// from choosing `1234` in the first place, which is where most of the real
/// risk lives.
enum PinProblem {
  /// Not yet the required number of digits.
  incomplete,

  /// Contains something that is not a digit.
  notNumeric,

  /// Every digit the same: 0000, 1111.
  repeated,

  /// Runs in sequence, up or down: 1234, 4321.
  sequential,

  /// The two entries do not match. Only used when confirming.
  mismatch,
}

@immutable
class MemberPin {
  const MemberPin._(this.value);

  final String value;

  static final RegExp _digits = RegExp(r'^[0-9]+$');

  /// Validate a PIN for SENDING. Applies length and digits only.
  ///
  /// The weak-value rules are deliberately NOT applied here: a member whose
  /// existing PIN is `1234` must still be able to sign in with it. Refusing
  /// their real PIN at the login screen would lock them out of their own
  /// money to make a point. [choose] is where the rules apply, because that
  /// is where the value is being decided.
  static ({MemberPin? pin, PinProblem? problem}) parse(
    String raw, {
    required int length,
  }) {
    if (!_digits.hasMatch(raw)) {
      return (pin: null, problem: PinProblem.notNumeric);
    }
    if (raw.length != length) {
      return (pin: null, problem: PinProblem.incomplete);
    }
    return (pin: MemberPin._(raw), problem: null);
  }

  /// Validate a PIN being SET or RESET, with the weak-value rules applied.
  static ({MemberPin? pin, PinProblem? problem}) choose(
    String raw,
    String confirmation, {
    required int length,
  }) {
    final ({MemberPin? pin, PinProblem? problem}) parsed =
        parse(raw, length: length);
    if (parsed.pin == null) {
      return parsed;
    }
    if (isRepeated(raw)) {
      return (pin: null, problem: PinProblem.repeated);
    }
    if (isSequential(raw)) {
      return (pin: null, problem: PinProblem.sequential);
    }
    if (raw != confirmation) {
      return (pin: null, problem: PinProblem.mismatch);
    }
    return parsed;
  }

  @visibleForTesting
  static bool isRepeated(String value) =>
      value.isNotEmpty && value.split('').toSet().length == 1;

  /// Runs by one in either direction. `1234` and `4321`, not `1357`.
  @visibleForTesting
  static bool isSequential(String value) {
    if (value.length < 2) {
      return false;
    }
    final int step = value.codeUnitAt(1) - value.codeUnitAt(0);
    if (step != 1 && step != -1) {
      return false;
    }
    for (int i = 2; i < value.length; i++) {
      if (value.codeUnitAt(i) - value.codeUnitAt(i - 1) != step) {
        return false;
      }
    }
    return true;
  }
}
