// GENERATED FILE -- DO NOT EDIT.
//
// Produced by mobile/scripts/generate_api_models.py from
// web/packages/api-client/openapi.json, the binding contract.
// Run that script and commit the result; mobile:codegen-drift fails
// the pipeline on any hand-edit or stale snapshot.
//
// Scope: the schemas reachable from the /member surface, and nothing
// else. The member client cannot name a staff shape.
//
// Member paths in this snapshot:
//   /member/auth/otp/request
//   /member/auth/otp/verify
//   /member/auth/refresh
//   /member/guarantees/{guarantee_id}/consent
//   /member/guarantees/{guarantee_id}/release

import 'package:meta/meta.dart';
/// `GuaranteeOut` from the OpenAPI snapshot.
@immutable
class GuaranteeOut {
  const GuaranteeOut({
    required this.amount,
    this.applicationId,
    required this.borrowerMemberId,
    required this.guarantorMemberId,
    required this.id,
    this.loanId,
    required this.status,
    required this.version,
  });

  final String amount;
  final String? applicationId;
  final String borrowerMemberId;
  final String guarantorMemberId;
  final String id;
  final String? loanId;
  final String status;
  final int version;

  factory GuaranteeOut.fromJson(Map<String, Object?> json) => GuaranteeOut(
        amount: json['amount'] as String,
        applicationId: json['application_id'] as String?,
        borrowerMemberId: json['borrower_member_id'] as String,
        guarantorMemberId: json['guarantor_member_id'] as String,
        id: json['id'] as String,
        loanId: json['loan_id'] as String?,
        status: json['status'] as String,
        version: json['version'] as int,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'amount': amount,
        'application_id': applicationId,
        'borrower_member_id': borrowerMemberId,
        'guarantor_member_id': guarantorMemberId,
        'id': id,
        'loan_id': loanId,
        'status': status,
        'version': version,
      };
}

/// `HTTPValidationError` from the OpenAPI snapshot.
@immutable
class HTTPValidationError {
  const HTTPValidationError({
    this.detail,
  });

  final List<ValidationError>? detail;

  factory HTTPValidationError.fromJson(Map<String, Object?> json) => HTTPValidationError(
        detail: json['detail'] == null ? null : (json['detail']! as List<Object?>).map((Object? e) => ValidationError.fromJson(e! as Map<String, Object?>)).toList(growable: false),
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'detail': detail?.map((Object? e) => (e! as dynamic).toJson()).toList(growable: false),
      };
}

/// Consent/self-release body: the optimistic-lock version ONLY.
///
/// extra="forbid" (least disclosure v1.1): there is deliberately NO field for
/// who consents — the principal IS the authenticated credential; a
/// caller-asserted identity or consent flag is a rejected design (the lesson).
@immutable
class MemberActBody {
  const MemberActBody({
    required this.version,
  });

  final int version;

  factory MemberActBody.fromJson(Map<String, Object?> json) => MemberActBody(
        version: json['version'] as int,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'version': version,
      };
}

/// Request a one-time password for a sign-in identifier.
@immutable
class OtpRequestBody {
  const OtpRequestBody({
    this.email,
    this.identifier,
  });

  final String? email;
  final String? identifier;

  factory OtpRequestBody.fromJson(Map<String, Object?> json) => OtpRequestBody(
        email: json['email'] as String?,
        identifier: json['identifier'] as String?,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'email': email,
        'identifier': identifier,
      };
}

/// Verify the six-digit one-time password for a sign-in identifier.
@immutable
class OtpVerifyBody {
  const OtpVerifyBody({
    required this.code,
    this.email,
    this.identifier,
  });

  final String code;
  final String? email;
  final String? identifier;

  factory OtpVerifyBody.fromJson(Map<String, Object?> json) => OtpVerifyBody(
        code: json['code'] as String,
        email: json['email'] as String?,
        identifier: json['identifier'] as String?,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'code': code,
        'email': email,
        'identifier': identifier,
      };
}

/// `RefreshBody` from the OpenAPI snapshot.
@immutable
class RefreshBody {
  const RefreshBody({
    required this.refreshToken,
  });

  final String refreshToken;

  factory RefreshBody.fromJson(Map<String, Object?> json) => RefreshBody(
        refreshToken: json['refresh_token'] as String,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'refresh_token': refreshToken,
      };
}

/// `TokenResponse` from the OpenAPI snapshot.
@immutable
class TokenResponse {
  const TokenResponse({
    required this.accessToken,
    required this.expiresIn,
    required this.refreshToken,
  });

  final String accessToken;
  final int expiresIn;
  final String refreshToken;

  factory TokenResponse.fromJson(Map<String, Object?> json) => TokenResponse(
        accessToken: json['access_token'] as String,
        expiresIn: json['expires_in'] as int,
        refreshToken: json['refresh_token'] as String,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'access_token': accessToken,
        'expires_in': expiresIn,
        'refresh_token': refreshToken,
      };
}

/// `ValidationError` from the OpenAPI snapshot.
@immutable
class ValidationError {
  const ValidationError({
    this.ctx,
    this.input,
    required this.loc,
    required this.msg,
    required this.type,
  });

  final Map<String, Object?>? ctx;
  final Object? input;
  final List<Object?> loc;
  final String msg;
  final String type;

  factory ValidationError.fromJson(Map<String, Object?> json) => ValidationError(
        ctx: json['ctx'] as Map<String, Object?>?,
        input: json['input'],
        loc: (json['loc']! as List<Object?>).map((Object? e) => e).toList(growable: false),
        msg: json['msg'] as String,
        type: json['type'] as String,
      );

  Map<String, Object?> toJson() => <String, Object?>{
        'ctx': ctx,
        'input': input,
        'loc': loc,
        'msg': msg,
        'type': type,
      };
}
