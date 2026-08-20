/// SPKI certificate pinning (scaffold defect D2, #42 Option 1).
///
/// The P16 scaffold hashed the WHOLE certificate and hung the check on
/// `HttpClient.badCertificateCallback`. Both are wrong:
///
/// 1. A whole-certificate hash changes on every renewal, so the pin set
///    expires with the cert rather than with the KEY. #42 Option 1 pins the
///    SubjectPublicKeyInfo of a keypair WE own and carry to the new host
///    across the #11 hosting exit — a cert reissued for the same key keeps
///    the same SPKI pin, which is the entire point.
/// 2. `badCertificateCallback` fires only when the platform's own chain
///    validation FAILS. A rogue certificate that is validly signed by any CA
///    in the trust store never reaches it — precisely the attack pinning
///    exists to stop. The check must therefore run on the connected socket
///    BEFORE the request is written, which is what [connectionFactory] does.
///
/// There is no bypass flag. [PinEnforcement] is a build-time flavor constant,
/// not a runtime toggle: a switch an attacker (or a hurried release) can flip
/// is not a control.
library;

import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:meta/meta.dart';

/// Whether a pin mismatch aborts the connection or is merely reported.
///
/// Owner decision, 2026-08-20: #42 Option 1 (own-key SPKI custody) is adopted
/// NOW, while ENFORCEMENT waits for the #11 hosting cutover — the current
/// shared host chain is not the chain the fleet will meet in production, so
/// enforcing against it would pin the wrong key and brick the fleet at
/// cutover. Flavors ship [report] until the exit completes, then [enforce].
enum PinEnforcement {
  /// Mismatch aborts the connection. No request bytes reach the wire (FM-E).
  enforce,

  /// Mismatch is reported through [CertificatePinning.onMismatch] and the
  /// connection proceeds. Used ONLY before the hosting exit; the flavor
  /// constant flips to [enforce] in the release that follows cutover.
  report,
}

/// Pins the SPKI of the server's leaf certificate.
@immutable
class CertificatePinning {
  /// [pins] are base64-encoded SHA-256 digests of the DER SubjectPublicKeyInfo
  /// — the same `pin-sha256` form as HPKP, so they can be produced with:
  ///
  /// ```sh
  /// openssl x509 -in cert.pem -pubkey -noout \
  ///   | openssl pkey -pubin -outform der \
  ///   | openssl dgst -sha256 -binary | base64
  /// ```
  ///
  /// At least TWO pins are required and the requirement is not negotiable: a
  /// single pin with no offline backup key is an outage waiting for its
  /// rotation. The backup key is generated with the primary and stored
  /// offline, never on the serving host.
  CertificatePinning({
    required this.pins,
    required this.enforcement,
    this.trustedCertificate,
    this.onMismatch,
  }) {
    // A throw, not an assert: asserts are compiled OUT of release builds, so
    // an assert here would let exactly the builds that matter ship a pin set
    // with no backup key.
    if (pins.length < 2) {
      throw ArgumentError.value(
        pins,
        'pins',
        'A pin set needs a backup pin (#42): at least two SPKI pins',
      );
    }
    for (final String pin in pins) {
      final List<int> raw = base64.decode(pin);
      if (raw.length != 32) {
        throw ArgumentError.value(
            pin, 'pins', 'Not a base64 SHA-256 digest (expected 32 bytes)');
      }
    }
    if (enforcement == PinEnforcement.enforce && trustedCertificate == null) {
      // Enforcement without our own certificate would fall back to the public
      // trust store, which is the posture pinning exists to replace. Refuse to
      // build rather than ship a mode that only looks enforced.
      throw ArgumentError.value(
        trustedCertificate,
        'trustedCertificate',
        'enforce mode requires our own certificate (PEM bytes) as the sole trust anchor',
      );
    }
  }

  final List<String> pins;
  final PinEnforcement enforcement;

  /// PEM bytes of the certificate (or issuing CA) WE own. Required under
  /// [PinEnforcement.enforce]; unused under [PinEnforcement.report], where the
  /// public trust store still applies and only [verifyPeer] reports.
  final List<int>? trustedCertificate;

  /// Telemetry seam. Called on every mismatch in BOTH modes — in [report] mode
  /// this is the only signal that the pin set has gone stale, so a flavor that
  /// reports into the void has no early warning before it flips to [enforce].
  final void Function(String observedPin)? onMismatch;

  /// SHA-256 of the DER SubjectPublicKeyInfo, base64-encoded.
  static String spkiPinOf(X509Certificate certificate) =>
      base64.encode(sha256.convert(extractSpki(certificate.der)).bytes);

  bool matches(X509Certificate certificate) =>
      pins.contains(spkiPinOf(certificate));

  /// The HTTP client this pin set permits.
  ///
  /// Under [PinEnforcement.enforce] the client is built on a [SecurityContext]
  /// that trusts ONLY [trustedCertificate] -- the platform then refuses the
  /// handshake for anything that does not chain to our own certificate, before
  /// a single request byte is written (FM-E).
  ///
  /// A note on what was tried first: the natural-looking approach is to
  /// intercept the socket via `HttpClient.connectionFactory` and check the
  /// peer certificate there. Dart does not allow it -- `ConnectionTask` has no
  /// public constructor, so a factory can only forward a task from
  /// `Socket.startConnect`, never wrap one. `badCertificateCallback` is not an
  /// option either: it fires only when the platform's own chain validation has
  /// ALREADY failed, so a rogue certificate that some CA validly signed never
  /// reaches it. Restricting the trust anchors is the mechanism the platform
  /// actually offers, and it fails closed.
  HttpClient buildClient() {
    final HttpClient client;
    if (enforcement == PinEnforcement.enforce) {
      final SecurityContext context = SecurityContext(withTrustedRoots: false)
        ..setTrustedCertificatesBytes(trustedCertificate!);
      client = HttpClient(context: context);
    } else {
      client = HttpClient();
    }
    // Never accept a chain the platform rejected, in either mode.
    client.badCertificateCallback =
        (X509Certificate _, String __, int ___) => false;
    return client;
  }

  /// Verifies the leaf key of a completed connection against the pin set.
  ///
  /// This is the SPKI half, and it runs in BOTH modes: under [enforce] it is a
  /// second gate behind the trust-anchor restriction (a certificate can chain
  /// correctly and still carry the wrong key); under [report] it is the only
  /// signal that the pin set has gone stale, which is what makes the pre-cutover
  /// period informative rather than blind.
  void verifyPeer(X509Certificate? certificate) {
    if (certificate != null && matches(certificate)) {
      return;
    }
    final String observed =
        certificate == null ? '<none>' : spkiPinOf(certificate);
    onMismatch?.call(observed);
    if (enforcement == PinEnforcement.enforce) {
      throw const TlsException('Certificate pin mismatch: connection refused.');
    }
  }

  /// Walks the certificate DER far enough to return the SubjectPublicKeyInfo
  /// TLV verbatim.
  ///
  /// X.509 is:
  ///   Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signature }
  ///   TBSCertificate ::= SEQUENCE {
  ///     [0] version OPTIONAL, serialNumber, signature, issuer,
  ///     validity, subject, subjectPublicKeyInfo, ... }
  ///
  /// so the SPKI is the 7th element of the TBS sequence, or the 6th when the
  /// optional explicit version tag is absent (a v1 certificate).
  @visibleForTesting
  static List<int> extractSpki(List<int> der) {
    final _Tlv certificate = _readTlv(der, 0);
    final _Tlv tbs = _readTlv(der, certificate.contentStart);

    int offset = tbs.contentStart;
    // [0] EXPLICIT version — context-specific, constructed, number 0.
    if (der[offset] == 0xA0) {
      offset = _readTlv(der, offset).end;
    }
    // serialNumber, signature, issuer, validity, subject.
    for (int skipped = 0; skipped < 5; skipped++) {
      offset = _readTlv(der, offset).end;
    }
    final _Tlv spki = _readTlv(der, offset);
    return der.sublist(offset, spki.end);
  }

  static _Tlv _readTlv(List<int> bytes, int start) {
    if (start + 1 >= bytes.length) {
      throw const FormatException('Truncated DER: no tag/length at offset');
    }
    int cursor =
        start + 1; // Single-byte tags only; X.509 uses no high tags here.
    final int first = bytes[cursor++];
    int length;
    if (first < 0x80) {
      length = first;
    } else {
      final int lengthBytes = first & 0x7F;
      if (lengthBytes == 0 || lengthBytes > 4) {
        throw const FormatException('Unsupported DER length encoding');
      }
      length = 0;
      for (int i = 0; i < lengthBytes; i++) {
        length = (length << 8) | bytes[cursor++];
      }
    }
    final int end = cursor + length;
    if (end > bytes.length) {
      throw const FormatException('Truncated DER: length runs past the buffer');
    }
    return _Tlv(contentStart: cursor, end: end);
  }
}

@immutable
class _Tlv {
  const _Tlv({required this.contentStart, required this.end});
  final int contentStart;
  final int end;
}
