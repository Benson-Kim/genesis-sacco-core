/// Authentication models — generated from OpenAPI spec.
/// Hand-written stub until P3 backend is available and spec is published.
library;

class OtpChallengeRequest {
  const OtpChallengeRequest({required this.phone});
  final String phone;

  Map<String, dynamic> toJson() => {'phone': phone};
}

class OtpVerifyRequest {
  const OtpVerifyRequest({required this.phone, required this.otp});
  final String phone;
  final String otp;

  Map<String, dynamic> toJson() => {'phone': phone, 'otp': otp};
}

class TokenPair {
  const TokenPair({required this.accessToken, required this.refreshToken});

  factory TokenPair.fromJson(Map<String, dynamic> json) => TokenPair(
        accessToken: json['access_token'] as String,
        refreshToken: json['refresh_token'] as String,
      );

  final String accessToken;
  final String refreshToken;
}

class RefreshRequest {
  const RefreshRequest({required this.refreshToken});
  final String refreshToken;

  Map<String, dynamic> toJson() => {'refresh_token': refreshToken};
}

class MePermissions {
  const MePermissions({required this.permissions});

  factory MePermissions.fromJson(Map<String, dynamic> json) => MePermissions(
        permissions: (json['permissions'] as List<dynamic>).cast<String>(),
      );

  final List<String> permissions;
}
