/// [CredentialPort] against endpoints that DO NOT EXIST YET.
///
/// Every path in this file is a proposal. `develop` carries OTP-only sign in
/// and nothing else on the member auth surface. The shapes here are the ones
/// `docs/technical/member-auth-contract-proposal.md` puts to whoever builds
/// the server half; when the real contract lands, this file is where it
/// arrives, and the controllers and screens above it should not need to move.
///
/// The proposed routes all sit under `/member/`, so the transport's path
/// guard and the CI staff-path sweep cover them exactly as they cover the
/// merged ones.
///
/// # Idempotency
///
/// A sign in attempt is a mutation: it burns an attempt against a lockout
/// counter and dispatches an SMS that may cost money. Double-tapping the
/// button must not do either twice. The key rotates when the credentials
/// change, which is what makes a corrected PIN a new attempt rather than a
/// replay of the failed one.
library;

import 'dart:convert';

import 'package:gp_api_client/gp_api_client.dart';

import '../../../core/session.dart';
import '../domain/credential_port.dart';
import '../domain/member_credential.dart';

const String _signInPath = '/member/auth/sign-in';
const String _verifyPath = '/member/auth/challenge/verify';
const String _resendPath = '/member/auth/challenge/resend';
const String _resetRequestPath = '/member/auth/pin/reset/request';
const String _resetPath = '/member/auth/pin/reset';
const String _initialPinPath = '/member/auth/pin/initial';

class MemberCredentialRepository implements CredentialPort {
  MemberCredentialRepository(this._client);

  final GpHttpClient _client;

  final IdempotencyKeySlot _signInSlot = IdempotencyKeySlot();
  final IdempotencyKeySlot _verifySlot = IdempotencyKeySlot();
  final IdempotencyKeySlot _resendSlot = IdempotencyKeySlot();
  final IdempotencyKeySlot _resetSlot = IdempotencyKeySlot();

  @override
  Future<OtpChallenge> signIn(MemberNumber number, MemberPin pin) async {
    final Map<String, Object?> body = <String, Object?>{
      'member_no': number.value,
      'pin': pin.value,
    };
    return _challenge(
      await _client.post(
        _signInPath,
        body: body,
        idempotencyKey: _signInSlot.keyFor(jsonEncode(body)),
      ),
    );
  }

  @override
  Future<TokenPair> verify(OtpChallenge challenge, String code) async {
    final Map<String, Object?> body = <String, Object?>{
      'challenge_id': challenge.id,
      'code': code,
    };
    final Map<String, Object?> response = await _client.post(
      _verifyPath,
      body: body,
      idempotencyKey: _verifySlot.keyFor(jsonEncode(body)),
    );
    return _tokens(response);
  }

  @override
  Future<OtpChallenge> resend(OtpChallenge challenge) async {
    final Map<String, Object?> body = <String, Object?>{
      'challenge_id': challenge.id,
    };
    // A resend is always a NEW intent: the member is asking for another code
    // because the first did not arrive. Reusing the key would replay the
    // first dispatch and send nothing, which is the same trap the OTP-only
    // flow has on its request route.
    _resendSlot.reset();
    return _challenge(
      await _client.post(
        _resendPath,
        body: body,
        idempotencyKey: _resendSlot.keyFor(jsonEncode(body)),
      ),
    );
  }

  @override
  Future<OtpChallenge> requestPinReset(MemberNumber number) async {
    final Map<String, Object?> body = <String, Object?>{
      'member_no': number.value,
    };
    return _challenge(
      await _client.post(
        _resetRequestPath,
        body: body,
        idempotencyKey: _resetSlot.keyFor(jsonEncode(body)),
      ),
    );
  }

  @override
  Future<void> resetPin(
    OtpChallenge challenge,
    String code,
    MemberPin pin,
  ) async {
    final Map<String, Object?> body = <String, Object?>{
      'challenge_id': challenge.id,
      'code': code,
      'pin': pin.value,
    };
    await _client.postVoid(
      _resetPath,
      body: body,
      idempotencyKey: _verifySlot.keyFor(jsonEncode(body)),
    );
  }

  @override
  Future<TokenPair> setInitialPin(
    OtpChallenge challenge,
    String code,
    MemberPin pin,
  ) async {
    final Map<String, Object?> body = <String, Object?>{
      'challenge_id': challenge.id,
      'code': code,
      'pin': pin.value,
    };
    return _tokens(
      await _client.post(
        _initialPinPath,
        body: body,
        idempotencyKey: _verifySlot.keyFor(jsonEncode(body)),
      ),
    );
  }

  /// A 2xx of the wrong shape is a failed call, not a crash.
  static OtpChallenge _challenge(Map<String, Object?> json) {
    final Object? id = json['challenge_id'];
    final Object? expires = json['expires_at'];
    if (id is! String || expires is! String) {
      throw const ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: 200,
      );
    }
    final DateTime? deadline = DateTime.tryParse(expires);
    if (deadline == null) {
      throw const ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: 200,
      );
    }
    final Object? destination = json['destination'];
    return OtpChallenge(
      id: id,
      // Absent on the reset route by design: that one cannot verify anything
      // first, so naming the destination would confirm the member number is
      // real. An empty string means "we are not saying", and the screen
      // hedges accordingly.
      destination: destination is String ? destination : '',
      expiresAt: deadline.toLocal(),
    );
  }

  static TokenPair _tokens(Map<String, Object?> response) {
    final TokenResponse token;
    try {
      token = TokenResponse.fromJson(response);
    } on TypeError {
      throw const ApiError(
        kind: ApiFailureKind.malformedResponse,
        statusCode: 200,
      );
    }
    return TokenPair(
      accessToken: token.accessToken,
      refreshToken: token.refreshToken,
      expiresIn: Duration(seconds: token.expiresIn),
    );
  }
}
