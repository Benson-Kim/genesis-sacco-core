/// Test doubles shared by the golden renders and the widget tests.
///
/// They implement the real ports, so `implements` makes any drift from the
/// interface a compile error rather than a test that quietly stops exercising
/// the thing it claims to.
///
/// Nothing here is imported from `lib/`, and nothing here has an equivalent
/// in `lib/`. A fake that lives beside production code is a fake that ends up
/// wired into it.
library;

import 'dart:async';

import 'package:gp_api_client/gp_api_client.dart';
import 'package:member_app/src/core/session.dart';
import 'package:member_app/src/features/auth/domain/auth_port.dart';
import 'package:member_app/src/features/auth/domain/credential_port.dart';
import 'package:member_app/src/features/auth/domain/member_credential.dart';
import 'package:member_app/src/features/auth/domain/sign_in_identifier.dart';
import 'package:member_app/src/features/guarantees/domain/guarantee_port.dart';

/// An [AuthPort] that answers instantly and can be told to refuse.
class FakeAuthPort implements AuthPort {
  FakeAuthPort({this.rejectCode = false});

  /// Makes `verifyOtp` answer the way a wrong code does.
  final bool rejectCode;

  int requestCalls = 0;
  int verifyCalls = 0;
  final List<bool> freshIntents = <bool>[];

  @override
  Future<void> requestOtp(
    SignInIdentifier identifier, {
    bool freshIntent = false,
  }) async {
    requestCalls++;
    freshIntents.add(freshIntent);
  }

  @override
  Future<TokenPair> verifyOtp(SignInIdentifier identifier, String code) async {
    verifyCalls++;
    if (rejectCode) {
      throw const ApiError(
        kind: ApiFailureKind.unauthenticated,
        statusCode: 401,
        category: 'unauthenticated',
      );
    }
    return const TokenPair(
      accessToken: 'header.payload.signature',
      refreshToken: 'r-1',
      expiresIn: Duration(seconds: 900),
    );
  }

  @override
  Future<TokenPair> refresh(String refreshToken) async => const TokenPair(
        accessToken: 'header.payload.signature',
        refreshToken: 'r-2',
        expiresIn: Duration(seconds: 900),
      );
}

/// A [CredentialPort] for the two-factor flow whose endpoints do not exist.
///
/// Answers instantly, and can be told to refuse the way the proposed contract
/// says the server will: one indistinguishable refusal for a wrong PIN and an
/// unknown member number, and a separate one for lockout.
class FakeCredentialPort implements CredentialPort {
  FakeCredentialPort({
    this.rejectCredentials = false,
    this.lockedOut = false,
    this.rejectCode = false,
    this.now,
  });

  final bool rejectCredentials;
  final bool lockedOut;
  final bool rejectCode;

  /// Anchors the challenge expiry, so a countdown renders the same every run.
  final DateTime? now;

  int signInCalls = 0;
  int resendCalls = 0;

  OtpChallenge get _challenge => OtpChallenge(
        id: 'challenge-1',
        destination: '07XX XXX 678',
        expiresAt: (now ?? DateTime.now()).add(const Duration(minutes: 5)),
      );

  @override
  Future<OtpChallenge> signIn(MemberNumber number, MemberPin pin) async {
    signInCalls++;
    if (lockedOut) {
      throw const ApiError(
        kind: ApiFailureKind.forbidden,
        statusCode: 403,
        correlationId: 'c-locked',
      );
    }
    if (rejectCredentials) {
      throw const ApiError(
        kind: ApiFailureKind.unauthenticated,
        statusCode: 401,
      );
    }
    return _challenge;
  }

  @override
  Future<TokenPair> verify(OtpChallenge challenge, String code) async {
    if (rejectCode) {
      throw const ApiError(
        kind: ApiFailureKind.unauthenticated,
        statusCode: 401,
      );
    }
    return const TokenPair(
      accessToken: 'header.payload.signature',
      refreshToken: 'r-1',
      expiresIn: Duration(seconds: 900),
    );
  }

  @override
  Future<OtpChallenge> resend(OtpChallenge challenge) async {
    resendCalls++;
    return _challenge;
  }

  @override
  Future<OtpChallenge> requestPinReset(MemberNumber number) async =>
      OtpChallenge(
        id: 'reset-1',
        // Empty on purpose: this route cannot verify anything first, so
        // naming the destination would confirm the member number is real.
        destination: '',
        expiresAt: (now ?? DateTime.now()).add(const Duration(minutes: 5)),
      );

  @override
  Future<void> resetPin(
    OtpChallenge challenge,
    String code,
    MemberPin pin,
  ) async {
    if (rejectCode) {
      throw const ApiError(
        kind: ApiFailureKind.unauthenticated,
        statusCode: 401,
      );
    }
  }

  @override
  Future<TokenPair> setInitialPin(
    OtpChallenge challenge,
    String code,
    MemberPin pin,
  ) async =>
      const TokenPair(
        accessToken: 'header.payload.signature',
        refreshToken: 'r-1',
        expiresIn: Duration(seconds: 900),
      );
}

/// A [GuaranteePort] that can succeed, or fail the version fence.
class FakeGuaranteePort implements GuaranteePort {
  FakeGuaranteePort({
    this.stale = false,
    this.forbidden = false,
    this.gate,
  });

  /// Holds the call open, so an in-flight state can be observed. A gate on
  /// the fake rather than a subclass that wraps it: a subclass overriding
  /// `act` would have to remember not to double count the call, and the one
  /// that forgot would make the double submit guard look like it worked.
  final Completer<void>? gate;

  /// Answers the way the optimistic lock does when the row has moved.
  final bool stale;

  /// Answers the way the server does for every wrong-actor shape at once.
  final bool forbidden;

  int calls = 0;
  final List<String> keysSeen = <String>[];

  @override
  Future<GuaranteeOut> act(
    String guaranteeId,
    GuaranteeAct act, {
    required int version,
  }) async {
    calls++;
    await gate?.future;
    if (stale) {
      throw const ApiError(
        kind: ApiFailureKind.conflict,
        statusCode: 409,
        category: 'conflict',
        correlationId: 'c-409',
      );
    }
    if (forbidden) {
      throw const ApiError(
        kind: ApiFailureKind.forbidden,
        statusCode: 403,
        category: 'forbidden',
      );
    }
    return GuaranteeOut(
      amount: '150000.00',
      borrowerMemberId: 'a41b90ce-0000-4000-8000-000000000002',
      guarantorMemberId: 'b52c81df-0000-4000-8000-000000000003',
      id: guaranteeId,
      loanId: null,
      applicationId: '7f3a1c22-0000-4000-8000-000000000001',
      // The server's word on the new state, which the client renders rather
      // than assuming. Release yields `released`, consent yields `active`.
      status: act == GuaranteeAct.consent ? 'active' : 'released',
      version: version + 1,
    );
  }
}
