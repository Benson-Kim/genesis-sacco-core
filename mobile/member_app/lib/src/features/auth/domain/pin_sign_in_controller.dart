/// The two-factor sign in: member number and PIN, then a code.
///
/// Sits alongside [SignInController] rather than replacing it. That one drives
/// the OTP-only flow that is merged and working; this one drives the flow the
/// backend is being built to. [Flavor.authMode] picks. Two controllers rather
/// than one with a mode switch, because the branches share almost nothing
/// except the code step — and the code step is a widget, not a branch.
///
/// # What the failure messages may and may not say
///
/// A rejected sign in gets ONE message, whatever went wrong. The server is
/// asked to answer identically for a wrong PIN and an unknown member number,
/// and this controller would not be able to tell them apart even if it
/// wanted to. Member numbers run in sequence, so a screen that distinguished
/// them would let anyone walk the range and read off which ones are real.
///
/// Lockout is the single exception, and only because the alternative is worse:
/// a member whose account is locked needs to be told to call the SACCO, not
/// left retrying a PIN that cannot work. That disclosure is deliberate, and it
/// is the only one.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:meta/meta.dart';

import '../../../core/env.dart';
import '../../../core/providers.dart';
import '../../../core/session.dart';
import 'credential_port.dart';
import 'member_credential.dart';

enum PinSignInStep {
  /// Member number and PIN.
  credentials,

  /// The dispatched code.
  code,

  /// Tokens adopted; the router takes over.
  done,
}

@immutable
class PinSignInState {
  const PinSignInState({
    this.step = PinSignInStep.credentials,
    this.busy = false,
    this.message,
    this.correlationId,
    this.challenge,
    this.lockedOut = false,
  });

  final PinSignInStep step;
  final bool busy;
  final String? message;
  final String? correlationId;

  /// Set once the PIN has been accepted. Carries the masked destination and
  /// the expiry the code step counts down to.
  final OtpChallenge? challenge;

  /// The one failure the member is told about specifically.
  final bool lockedOut;

  PinSignInState copyWith({
    PinSignInStep? step,
    bool? busy,
    String? message,
    String? correlationId,
    OtpChallenge? challenge,
    bool? lockedOut,
    bool clearMessage = false,
  }) =>
      PinSignInState(
        step: step ?? this.step,
        busy: busy ?? this.busy,
        challenge: challenge ?? this.challenge,
        lockedOut: lockedOut ?? this.lockedOut,
        message: clearMessage ? null : (message ?? this.message),
        correlationId:
            clearMessage ? null : (correlationId ?? this.correlationId),
      );
}

class PinSignInController extends Notifier<PinSignInState> {
  late CredentialPort _credentials;
  late MemberSession _session;
  late int _pinLength;

  @override
  PinSignInState build() {
    _credentials = ref.watch(credentialPortProvider);
    _session = ref.watch(sessionProvider);
    _pinLength = ref.watch(flavorProvider).pinLength;
    return const PinSignInState();
  }

  /// Step one. Validates locally, then verifies the PIN server-side.
  Future<void> submitCredentials(String rawNumber, String rawPin) async {
    if (state.busy) {
      return;
    }
    final ({MemberNumber? number, MemberNumberProblem? problem}) number =
        MemberNumber.parse(rawNumber);
    if (number.number == null) {
      state = state.copyWith(message: _numberMessage(number.problem!));
      return;
    }
    final ({MemberPin? pin, PinProblem? problem}) pin =
        MemberPin.parse(rawPin, length: _pinLength);
    if (pin.pin == null) {
      state = state.copyWith(
        message: 'Enter your $_pinLength digit PIN.',
      );
      return;
    }

    state = state.copyWith(busy: true, clearMessage: true);
    final OtpChallenge challenge;
    try {
      challenge = await _credentials.signIn(number.number!, pin.pin!);
    } on ApiError catch (error) {
      state = _failed(error);
      return;
    }
    state = state.copyWith(
      step: PinSignInStep.code,
      busy: false,
      challenge: challenge,
      clearMessage: true,
    );
  }

  /// Step two.
  Future<void> submitCode(String rawCode) async {
    final OtpChallenge? challenge = state.challenge;
    if (state.busy || challenge == null) {
      return;
    }
    final String code = rawCode.trim();
    if (!RegExp(r'^\d{6}$').hasMatch(code)) {
      state = state.copyWith(message: 'Enter the six digits from your code.');
      return;
    }
    state = state.copyWith(busy: true, clearMessage: true);
    final TokenPair pair;
    try {
      pair = await _credentials.verify(challenge, code);
    } on ApiError catch (error) {
      state = _failed(error, verifying: true);
      return;
    }
    // adopt() persists the refresh token before completing, so the flow
    // cannot reach `done` holding a token that was never written down.
    await _session.adopt(pair);
    state = state.copyWith(step: PinSignInStep.done, busy: false);
  }

  Future<void> resend() async {
    final OtpChallenge? challenge = state.challenge;
    if (state.busy || challenge == null) {
      return;
    }
    state = state.copyWith(busy: true, clearMessage: true);
    final OtpChallenge fresh;
    try {
      fresh = await _credentials.resend(challenge);
    } on ApiError catch (error) {
      state = _failed(error, verifying: true);
      return;
    }
    state = state.copyWith(busy: false, challenge: fresh, clearMessage: true);
  }

  /// Back to the credentials step, discarding the challenge.
  void restart() {
    state = const PinSignInState();
  }

  PinSignInState _failed(ApiError error, {bool verifying = false}) {
    switch (error.kind) {
      case ApiFailureKind.transport:
        return state.copyWith(
          busy: false,
          message: 'We could not reach Genesis Prestige. Check your '
              'connection and try again.',
        );
      case ApiFailureKind.rateLimited:
        return state.copyWith(
          busy: false,
          message: 'Too many attempts. Wait a minute, then try again.',
        );
      case ApiFailureKind.forbidden:
        // The proposed contract reserves 403 for lockout, and lockout alone.
        // It is the one refusal a member is told about specifically, because
        // retrying is genuinely hopeless and calling the SACCO is not.
        return state.copyWith(
          busy: false,
          lockedOut: true,
          message: 'Your account is locked for now. Please contact your '
              'SACCO to unlock it.',
          correlationId: error.correlationId,
        );
      case ApiFailureKind.unauthenticated:
        // One message for a wrong PIN and for a member number that does not
        // exist. The server does not tell this app which, on purpose.
        return state.copyWith(
          busy: false,
          message: verifying
              ? 'That code is not valid. Check it, or ask for a new one.'
              : 'That member number and PIN do not match.',
        );
      case ApiFailureKind.conflict:
      case ApiFailureKind.server:
      case ApiFailureKind.malformedResponse:
        return state.copyWith(
          busy: false,
          message: 'Something went wrong on our side. Please try again.',
          correlationId: error.correlationId,
        );
    }
  }

  static String _numberMessage(MemberNumberProblem problem) {
    switch (problem) {
      case MemberNumberProblem.empty:
        return 'Enter your member number.';
      case MemberNumberProblem.tooShort:
      case MemberNumberProblem.tooLong:
      case MemberNumberProblem.badCharacters:
        return 'That does not look like a member number. Check your '
            'passbook or statement.';
    }
  }
}

final NotifierProvider<PinSignInController, PinSignInState>
    pinSignInControllerProvider =
    NotifierProvider<PinSignInController, PinSignInState>(
  PinSignInController.new,
);
