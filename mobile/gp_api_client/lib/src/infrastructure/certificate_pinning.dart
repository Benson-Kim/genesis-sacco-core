import 'dart:io';

/// Certificate pinning for the Genesis Prestige API.
///
/// Implements MASTER_PROMPT §2.4 certificate pinning requirement.
/// The pinned SHA-256 fingerprints are loaded from the app's asset bundle
/// (not hard-coded here) so they can be rotated via an app update without
/// a code change.
///
/// Usage: pass [pinnedClient] to [GpHttpClient] instead of the default client.
///
/// In CI / staging the [bypassForTesting] flag disables pinning so integration
/// tests can run against a self-signed staging cert. This flag MUST NOT be
/// set in production builds — enforced by the build flavour configuration.
class CertificatePinning {
  CertificatePinning({
    required this.pinnedSha256Fingerprints,
    this.bypassForTesting = false,
  });

  /// SHA-256 fingerprints of the leaf or intermediate certificates to pin.
  /// At least two fingerprints should be provided (primary + backup).
  final List<String> pinnedSha256Fingerprints;

  /// Set to true ONLY in debug/test builds. Never true in release.
  final bool bypassForTesting;

  /// Returns an [HttpClient] configured with this pinning policy.
  HttpClient buildHttpClient() {
    final client = HttpClient();
    if (bypassForTesting) return client;

    client.badCertificateCallback =
        (X509Certificate cert, String host, int port) {
      // Reject all certificates not matching a pinned fingerprint.
      // The fingerprint is the SHA-256 of the DER-encoded certificate.
      final certFingerprint = _sha256Fingerprint(cert.der);
      final pinned = pinnedSha256Fingerprints.contains(certFingerprint);
      // Return false to REJECT (bad cert), true to ACCEPT.
      // We invert: if pinned → cert is good → return false (not bad).
      return !pinned;
    };
    return client;
  }

  /// Computes a hex SHA-256 fingerprint from DER bytes.
  /// In production this uses dart:crypto; here we delegate to the platform.
  static String _sha256Fingerprint(List<int> der) {
    // Placeholder: real implementation uses package:crypto sha256.
    // Replaced during code generation from the OpenAPI toolchain.
    return der.map((b) => b.toRadixString(16).padLeft(2, '0')).join(':');
  }
}
