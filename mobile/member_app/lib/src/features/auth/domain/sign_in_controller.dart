/// The sign-in flow: identifier -> code -> session.
///
/// This is where an [ApiError] stops being an [ApiError]. Screens receive
/// [SignInState] and render it; they never see a status code, a server
/// category string, or a kind enum. Two reasons, and the second is the one
/// that matters:
///
/// 1. A widget that switches on `error.category` has turned the server's
///    internal vocabulary into UI, and it breaks the day that vocabulary is
///    renamed.
/// 2. Gate 1.6. The server answers `POST /member/auth/otp/request` with
///    `{"status":"sent"}` no matter what — unknown identifier, dormant member,
///    wrong tenant, all identical, because telling them apart is an existence
///    oracle. Every message below is written to keep that true. A helpful
///    "we do not recognise that number" would hand an attacker the membership
///    register one guess at a time.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:meta/meta.dart';

import '../../../core/providers.dart';
import '../../../core/session.dart';
import 'auth_port.dart';
import 'sign_in_identifier.dart';

/// The server's own rule: `code: str = Field(pattern=r"^\d{6}$")`.
final RegExp _sixDigits = RegExp(r'^\d{6}$');

/// How long the UI refuses to ask for another code.
///
/// A courtesy to the member, and a guard on a sharp edge: the auth rate guard
/// answers 429, the idempotency middleware STORES that 429, and a stored
/// response replays for the rest of the retention window. A key that catches a
/// rate limit stays poisoned far longer than the rate limit itself lasts. The
/// repository rotates the key on every resend so no key is reused into that
/// state, and this window keeps the member from reaching it at all.
const Duration resendCooldown = Duration(seconds: 30);

enum SignInStep {
  /// Asking for an email address or a mobile number.
  identifier,

  /// A code has been requested; asking for the six digits.
  code,

  /// Tokens adopted. The router takes over from here.
  done,
}

@immutable
class SignInState {
  const SignInState({
    this.step = SignInStep.identifier,
    this.busy = false,
    this.message,
    this.correlationId,
    this.identifier,
    this.codeSentAt,
  });

  final SignInStep step;

  /// A call is in flight. Screens disable their submit control on this — the
  /// first half of FM-G, the other half being the Idempotency-Key that makes a
  /// double submit harmless when the control is somehow tapped twice anyway.
  final bool busy;

  /// Member-facing, written here, never the server's words.
  final String? message;

  /// The one server-provided string the UI renders verbatim, so support can
  /// find the request. Set only for failures the member cannot act on.
  final String? correlationId;

  final SignInIdentifier? identifier;

  /// When the last code was requested, for the resend cooldown.
  final DateTime? codeSentAt;

  SignInState copyWith({
    SignInStep? step,
    bool? busy,
    SignInIdentifier? identifier,
    DateTime? codeSentAt,
    String? message,
    String? correlationId,
    bool clearMessage = false,
  }) =>
      SignInState(
        step: step ?? this.step,
        busy: busy ?? this.busy,
        identifier: identifier ?? this.identifier,
        codeSentAt: codeSentAt ?? this.codeSentAt,
        message: clearMessage ? null : (message ?? this.message),
        correlationId:
            clearMessage ? null : (correlationId ?? this.correlationId),
      );
}

/// The flow, as the screens consume it.
final NotifierProvider<SignInController, SignInState> signInControllerProvider =
    NotifierProvider<SignInController, SignInState>(SignInController.new);

class SignInController extends Notifier<SignInState> {
  /// Dependencies are read in [build], not taken through the constructor:
  /// `NotifierProvider` builds its notifier with no arguments, and `ref` is
  /// the seam through which a test substitutes a fake [AuthPort] and a fixed
  /// clock. Deliberately not `late final` — [build] may run more than once,
  /// and a second assignment to a `late final` throws.
  late AuthPort _auth;
  late MemberSession _session;
  late DateTime Function() _now;

  @override
  SignInState build() {
    _auth = ref.watch(authPortProvider);
    _session = ref.watch(sessionProvider);
    _now = ref.watch(clockProvider);
    // A flow that reached `done` stays there until something resets it, and
    // the thing that must reset it is the session ending — an inactivity
    // logout, a 401, an explicit sign-out. Without this the member is returned
    // to a sign-in screen whose controller still believes it finished, and the
    // screen renders its post-adoption spinner forever.
    final StreamSubscription<SessionState> subscription =
        _session.states.listen((SessionState session) {
      if (session == SessionState.signedOut) {
        state = const SignInState();
      }
    });
    ref.onDispose(subscription.cancel);
    return const SignInState();
  }

  /// True when another code may be requested.
  bool get canResend {
    final DateTime? sent = state.codeSentAt;
    return sent == null || _now().difference(sent) >= resendCooldown;
  }

  /// Validate what the member typed, then ask for a code.
  Future<void> submitIdentifier(String raw) async {
    if (state.busy) {
      return;
    }
    final ({SignInIdentifier? identifier, IdentifierProblem? problem}) parsed =
        SignInIdentifier.parse(raw);
    final SignInIdentifier? identifier = parsed.identifier;
    if (identifier == null) {
      state = state.copyWith(message: _identifierMessage(parsed.problem!));
      return;
    }
    state =
        state.copyWith(busy: true, identifier: identifier, clearMessage: true);
    await _send(identifier, freshIntent: false);
  }

  /// Ask for another code for the identifier already entered.
  Future<void> resend() async {
    final SignInIdentifier? identifier = state.identifier;
    if (state.busy || identifier == null) {
      return;
    }
    if (!canResend) {
      state = state.copyWith(
        message: 'Hold on a moment before asking for another code.',
      );
      return;
    }
    state = state.copyWith(busy: true, clearMessage: true);
    // freshIntent: a resend is a NEW intent, so the Idempotency-Key rotates.
    // Reuse it and the server replays the previous {"status":"sent"} without
    // minting a second code, leaving the member waiting for a message that was
    // never going to be sent.
    await _send(identifier, freshIntent: true);
  }

  Future<void> _send(
    SignInIdentifier identifier, {
    required bool freshIntent,
  }) async {
    try {
      await _auth.requestOtp(identifier, freshIntent: freshIntent);
    } on ApiError catch (error) {
      state = _failed(error);
      return;
    }
    state = state.copyWith(
      step: SignInStep.code,
      busy: false,
      codeSentAt: _now(),
      clearMessage: true,
    );
  }

  /// Verify the six digits and adopt the resulting session.
  Future<void> submitCode(String rawCode) async {
    final SignInIdentifier? identifier = state.identifier;
    if (state.busy || identifier == null) {
      return;
    }
    final String code = rawCode.trim();
    if (!_sixDigits.hasMatch(code)) {
      state = state.copyWith(message: 'Enter the six digits from your code.');
      return;
    }
    state = state.copyWith(busy: true, clearMessage: true);
    final TokenPair pair;
    try {
      pair = await _auth.verifyOtp(identifier, code);
    } on ApiError catch (error) {
      state = _failed(error, verifying: true);
      return;
    }
    // adopt() persists the refresh token BEFORE it completes, so the flow
    // cannot reach `done` holding a token that was never written down.
    await _session.adopt(pair);
    state = state.copyWith(step: SignInStep.done, busy: false);
  }

  /// Back to the identifier field — a mistyped number, or a code that will
  /// never arrive because it went to an address the member no longer holds.
  void restart() {
    state = const SignInState();
  }

  /// [ApiError] -> something a member can read, and nothing more.
  ///
  /// Note what is NOT consulted: `error.category`. It is the server's private
  /// vocabulary; rendering it would leak that vocabulary AND pin the UI to it.
  /// The kind alone decides what the member should DO, which is the only
  /// question a message has to answer.
  SignInState _failed(ApiError error, {bool verifying = false}) {
    final String message;
    String? correlationId;
    switch (error.kind) {
      case ApiFailureKind.transport:
        message = 'We could not reach Genesis Prestige. '
            'Check your connection and try again.';
      case ApiFailureKind.rateLimited:
        message = 'Too many attempts. Wait a minute, then try again.';
      case ApiFailureKind.unauthenticated:
        // On verify this is a wrong or expired code, and the message says so
        // without implying anything about whether the identifier is known.
        // On request it means the tenant header was refused, which is a broken
        // build rather than anything the member did — so it takes the generic
        // arm, with a correlation id support can actually trace.
        if (verifying) {
          message = 'That code is not valid. Check it, or ask for a new one.';
        } else {
          message = _generic;
          correlationId = error.correlationId;
        }
      case ApiFailureKind.forbidden:
      case ApiFailureKind.conflict:
      case ApiFailureKind.server:
      case ApiFailureKind.malformedResponse:
        message = _generic;
        correlationId = error.correlationId;
    }
    return state.copyWith(
      busy: false,
      message: message,
      correlationId: correlationId,
    );
  }

  static const String _generic =
      'Something went wrong on our side. Please try again.';

  /// Local shape complaints. Each describes the text in the field, and nothing
  /// about the server's records.
  static String _identifierMessage(IdentifierProblem problem) {
    switch (problem) {
      case IdentifierProblem.empty:
        return 'Enter your mobile number or email address.';
      case IdentifierProblem.length:
        return 'That does not look like a mobile number or an email address.';
      case IdentifierProblem.malformedPhone:
        // The one worth spelling out: the server would take this string as an
        // EMAIL, resolve nothing, and answer "sent" exactly as it always does.
        return 'Enter a Kenyan mobile number as 07XX XXX XXX, '
            '01XX XXX XXX, or +2547XX XXX XXX.';
      case IdentifierProblem.malformedEmail:
        return 'Enter a complete email address, or a mobile number '
            'starting 07, 01 or +254.';
    }
  }
}
