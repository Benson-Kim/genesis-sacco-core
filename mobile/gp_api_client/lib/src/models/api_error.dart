/// The error envelope, decoded structurally and NEVER parsed for business
/// meaning (gate 1.6, least disclosure — the web doctrine carried over
/// verbatim from `web/src/lib/` error handling).
///
/// The server deliberately returns `{category, correlation_id}` and nothing
/// more: no balances, no vocabularies, no "which field was wrong". Any client
/// that branches on error TEXT is inventing a disclosure the server refused to
/// make, so this type exposes the category as an opaque string and the
/// correlation id for support, and offers no message parsing at all.
library;

import 'dart:convert';

import 'package:meta/meta.dart';

/// How a request failed. Deliberately coarse.
enum ApiFailureKind {
  /// The request never reached the server, or the TLS handshake was refused
  /// (certificate pinning rejection lands here — see [CertificatePinning]).
  transport,

  /// 401. Per `docs/member-auth.md`, ANY 401 ends the session: the client
  /// discards both tokens and returns to login. There is no refresh-on-401
  /// retry loop (scaffold defect D5).
  unauthenticated,

  /// 403. The member surface deliberately does not distinguish "not yours"
  /// from "not actionable" — the UI must not invent that distinction.
  forbidden,

  /// 409 — an optimistic-lock version was stale. Refetch and re-present.
  conflict,

  /// 429 — the member-read bucket (120/min at launch, #31) or the auth guard.
  rateLimited,

  /// Any other non-2xx.
  server,

  /// A 2xx body that could not be decoded into the expected shape. Surfaced
  /// as a typed failure rather than a raw decode crash (scaffold defect D7).
  malformedResponse,
}

/// A failed API call. Thrown by [GpHttpClient]; never constructed from a
/// guess about what the server "probably" meant.
@immutable
class ApiError implements Exception {
  const ApiError({
    required this.kind,
    required this.statusCode,
    this.category,
    this.correlationId,
  });

  final ApiFailureKind kind;

  /// Null when the failure happened before a response existed (transport).
  final int? statusCode;

  /// The server's opaque category string, when it sent one.
  final String? category;

  /// Shown to the member on the support screen so an operator can find the
  /// request in the logs. This is the ONLY server-provided string the UI
  /// renders verbatim.
  final String? correlationId;

  /// Decode an error body. A body that is empty, non-JSON, or JSON of the
  /// wrong shape is NOT an error in itself — it degrades to a categoryless
  /// [ApiError] for the status code, because a transport-level surprise must
  /// never crash the app on top of the failure it is already reporting.
  factory ApiError.fromResponse(int statusCode, String body) {
    String? category;
    String? correlationId;
    try {
      final Object? decoded = body.isEmpty ? null : jsonDecode(body);
      if (decoded is Map<String, Object?>) {
        final Object? c = decoded['category'];
        final Object? id = decoded['correlation_id'];
        category = c is String ? c : null;
        correlationId = id is String ? id : null;
      }
    } on FormatException {
      // Non-JSON error body. Nothing to salvage, nothing to crash over.
    }
    return ApiError(
      kind: kindForStatus(statusCode),
      statusCode: statusCode,
      category: category,
      correlationId: correlationId,
    );
  }

  static ApiFailureKind kindForStatus(int statusCode) {
    switch (statusCode) {
      case 401:
        return ApiFailureKind.unauthenticated;
      case 403:
        return ApiFailureKind.forbidden;
      case 409:
        return ApiFailureKind.conflict;
      case 429:
        return ApiFailureKind.rateLimited;
      default:
        return ApiFailureKind.server;
    }
  }

  /// NOTE: deliberately does not include [category] or [correlationId].
  /// `toString()` reaches crash reporters and log sinks, and FM-A requires
  /// that nothing request-identifying leaks into them.
  @override
  String toString() => 'ApiError(${kind.name}, status: $statusCode)';
}
