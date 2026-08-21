/// Forgot PIN: number, code, new PIN, done.
///
/// Four steps in the UI, three calls to the server. The code is not verified
/// on its own — step two only moves the screen forward; the code and the new
/// PIN are submitted together in step three.
///
/// That is deliberate and it is the security decision in this file. Verifying
/// a code as its own round trip would create a window in which a verified
/// code is spendable on its own, and a verified code that grants a PIN change
/// IS a credential. Keeping the exchange in one call means there is never a
/// moment where holding the code alone is worth anything.
///
/// The cost is that a wrong code is only discovered at the end, which is a
/// worse experience. It is the right trade: the failure is recoverable in
/// seconds and the window is not recoverable at all.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:meta/meta.dart';

import '../../../core/env.dart';
import '../../../core/providers.dart';
import 'credential_port.dart';
import 'member_credential.dart';

enum PinResetStep { number, code, newPin, done }

@immutable
class PinResetState {
  const PinResetState({
    this.step = PinResetStep.number,
    this.busy = false,
    this.message,
    this.correlationId,
    this.challenge,
    this.code,
  });

  final PinResetStep step;
  final bool busy;
  final String? message;
  final String? correlationId;
  final OtpChallenge? challenge;

  /// Held between step two and step three, because they are submitted
  /// together. In memory only, for the seconds it takes to choose a PIN.
  final String? code;

  PinResetState copyWith({
    PinResetStep? step,
    bool? busy,
    String? message,
    String? correlationId,
    OtpChallenge? challenge,
    String? code,
    bool clearMessage = false,
  }) =>
      PinResetState(
        step: step ?? this.step,
        busy: busy ?? this.busy,
        challenge: challenge ?? this.challenge,
        code: code ?? this.code,
        message: clearMessage ? null : (message ?? this.message),
        correlationId:
            clearMessage ? null : (correlationId ?? this.correlationId),
      );
}

class PinResetController extends Notifier<PinResetState> {
  late CredentialPort _credentials;
  late int _pinLength;

  @override
  PinResetState build() {
    _credentials = ref.watch(credentialPortProvider);
    _pinLength = ref.watch(flavorProvider).pinLength;
    return const PinResetState();
  }

  Future<void> submitNumber(String rawNumber) async {
    if (state.busy) {
      return;
    }
    final ({MemberNumber? number, MemberNumberProblem? problem}) parsed =
        MemberNumber.parse(rawNumber);
    if (parsed.number == null) {
      state = state.copyWith(message: 'Enter your member number.');
      return;
    }
    state = state.copyWith(busy: true, clearMessage: true);
    final OtpChallenge challenge;
    try {
      challenge = await _credentials.requestPinReset(parsed.number!);
    } on ApiError catch (error) {
      state = _failed(error);
      return;
    }
    state = state.copyWith(
      step: PinResetStep.code,
      busy: false,
      challenge: challenge,
      clearMessage: true,
    );
  }

  /// Moves to the PIN step. Does NOT verify the code — see the library doc.
  void submitCode(String rawCode) {
    if (state.busy) {
      return;
    }
    final String code = rawCode.trim();
    if (!RegExp(r'^\d{6}$').hasMatch(code)) {
      state = state.copyWith(message: 'Enter the six digits from your code.');
      return;
    }
    state = state.copyWith(
      step: PinResetStep.newPin,
      code: code,
      clearMessage: true,
    );
  }

  Future<void> submitNewPin(String raw, String confirmation) async {
    final OtpChallenge? challenge = state.challenge;
    final String? code = state.code;
    if (state.busy || challenge == null || code == null) {
      return;
    }
    final ({MemberPin? pin, PinProblem? problem}) chosen =
        MemberPin.choose(raw, confirmation, length: _pinLength);
    if (chosen.pin == null) {
      state = state.copyWith(message: _pinMessage(chosen.problem!));
      return;
    }
    state = state.copyWith(busy: true, clearMessage: true);
    try {
      await _credentials.resetPin(challenge, code, chosen.pin!);
    } on ApiError catch (error) {
      state = _failed(error, atPin: true);
      return;
    }
    state = state.copyWith(step: PinResetStep.done, busy: false);
  }

  void restart() {
    state = const PinResetState();
  }

  PinResetState _failed(ApiError error, {bool atPin = false}) {
    switch (error.kind) {
      case ApiFailureKind.transport:
        return state.copyWith(
          busy: false,
          message: 'We could not reach Genesis Prestige. Nothing was '
              'changed. Check your connection and try again.',
        );
      case ApiFailureKind.rateLimited:
        return state.copyWith(
          busy: false,
          message: 'Too many attempts. Wait a minute, then try again.',
        );
      case ApiFailureKind.unauthenticated:
        // At the PIN step this means the code was wrong or has expired —
        // the only point at which it is checked. The member goes back to the
        // code step rather than being stranded on a PIN form that cannot
        // submit.
        return atPin
            ? state.copyWith(
                busy: false,
                step: PinResetStep.code,
                message: 'That code was not valid, or it has expired. '
                    'Check it and try again.',
              )
            : state.copyWith(busy: false, message: _generic);
      case ApiFailureKind.forbidden:
      case ApiFailureKind.conflict:
      case ApiFailureKind.server:
      case ApiFailureKind.malformedResponse:
        return state.copyWith(
          busy: false,
          message: _generic,
          correlationId: error.correlationId,
        );
    }
  }

  static const String _generic =
      'Something went wrong on our side. Nothing was changed.';

  String _pinMessage(PinProblem problem) {
    switch (problem) {
      case PinProblem.incomplete:
      case PinProblem.notNumeric:
        return 'Your PIN must be $_pinLength digits.';
      case PinProblem.repeated:
        return 'Choose a PIN that is not all the same digit.';
      case PinProblem.sequential:
        return 'Choose a PIN that does not run in sequence.';
      case PinProblem.mismatch:
        return 'Those two PINs do not match.';
    }
  }
}

final NotifierProvider<PinResetController, PinResetState>
    pinResetControllerProvider =
    NotifierProvider<PinResetController, PinResetState>(
  PinResetController.new,
);
