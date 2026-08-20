/// Falsifiable proof that the SPKI extractor agrees with the reference
/// implementation.
///
/// Oracle (hand-computed, recorded per MASTER_PROMPT section 4): the fixture
/// below is a real self-signed X.509 certificate. Its pin was produced by
/// OpenSSL, not by this code:
///
/// ```sh
/// openssl x509 -in c.pem -pubkey -noout ///   | openssl pkey -pubin -outform der ///   | openssl dgst -sha256 -binary | base64
/// # -> pZJFCyM40oaAvghY+yHSYLqJmF3fxkl90aPxUozBm3s=
/// ```
///
/// If `extractSpki` walks the DER wrongly - off-by-one on a field, mishandled
/// long-form length, forgetting the optional [0] version tag - the digest
/// changes and this test fails. That is the guard: delete the walk and the
/// test cannot pass by accident.
library;

import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';

/// A real certificate (RSA-2048, CN=pin-fixture), DER, base64-encoded.
const String _certificateDerBase64 =
    'MIIDDTCCAfWgAwIBAgIUKpKcTf4SIeaejRkTrzWUhgu0EmkwDQYJKoZIhvcNAQELBQAwFjEUMBIG'
    'A1UEAwwLcGluLWZpeHR1cmUwHhcNMjYwODIwMTk1OTA4WhcNMjYwODIxMTk1OTA4WjAWMRQwEgYD'
    'VQQDDAtwaW4tZml4dHVyZTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAMfSMNByCBL0'
    'RXsaeVzDQEN2K007PoeJHsA32zNFQuiLdfXxE0NGYUS7HxWPv3hPiLCZecfqTfYbWFS2n5sb+cyh'
    'tYDvEgck6h7XkmmaStlXPIsYHzlHbAWxh5PeUSzMD/4J6nIzv88WIRbdSmLjOsVPz3keVTxTvrsp'
    'e+rULZbGi/udVrSLW88/Bf395O6mxpLK0IT/eXyRRBIT095YkbmN05g+QbES75Bi8SHlQMfz49YJ'
    'D4YezmFT1B/u0QOY1eASd0E23GqDa7ntMM6hELRXeXgHUh/GL1EAMYr7J3QNv8TQosadgJKUlonH'
    'zIYOOkSYLuipEYhpdLg4kgilSJ8CAwEAAaNTMFEwHQYDVR0OBBYEFMaMoUX3zj2tQUuv8cPCgU5O'
    'QgaHMB8GA1UdIwQYMBaAFMaMoUX3zj2tQUuv8cPCgU5OQgaHMA8GA1UdEwEB/wQFMAMBAf8wDQYJ'
    'KoZIhvcNAQELBQADggEBAKyrqAL+pFT6zOj4swwpZ4dnfct4+XiOBOo+N5QfSHE5xe9N1h3yoYs9'
    '1OYL/jGkQhiXgbdHvWoNVaNbfMOuChb7yaXBUW+1ruqZ422vwcpFEc6hlbPyQ8E8KwBFkalcwlCD'
    'PAvZ97sgeHCnIHBsJXTsLLbbZ7yPK/f3YhTOH7BVI0jdGXJXqefg7AcP6ChER0aGODtsodnJMXhX'
    'kX/6caByG+8osIg/WY5ntW2peb+YbLFzPKJrzVG870f+gYqt37b2YNW0QxsmkdjI7qdEo0iXPihM'
    'JY/QjoK9rtTXEkzlUo4lO7e20yTM2m2wf5xtirbG6HtJh+7VMBlnjaD6utw=';

void main() {
  group('CertificatePinning.extractSpki', () {
    test('produces the pin OpenSSL produces for the same certificate', () {
      final List<int> der = base64.decode(_certificateDerBase64);

      final List<int> spki = CertificatePinning.extractSpki(der);
      final String pin = base64.encode(sha256.convert(spki).bytes);

      expect(pin, 'pZJFCyM40oaAvghY+yHSYLqJmF3fxkl90aPxUozBm3s=');
    });

    test('the extracted SPKI is a DER SEQUENCE, not the whole certificate', () {
      final List<int> der = base64.decode(_certificateDerBase64);

      final List<int> spki = CertificatePinning.extractSpki(der);

      expect(spki.first, 0x30, reason: 'SubjectPublicKeyInfo is a SEQUENCE');
      expect(spki.length, lessThan(der.length),
          reason: 'pinning the whole certificate is scaffold defect D2');
    });

    test('rejects truncated DER instead of reading past the buffer', () {
      final List<int> der = base64.decode(_certificateDerBase64);

      expect(
        () => CertificatePinning.extractSpki(der.sublist(0, 40)),
        throwsA(isA<FormatException>()),
      );
    });
  });

  group('CertificatePinning construction', () {
    final List<String> twoPins = <String>[
      base64.encode(List<int>.filled(32, 1)),
      base64.encode(List<int>.filled(32, 2)),
    ];

    test('refuses a single pin even in a release build (throw, not assert)', () {
      // An un-rotatable pin is an outage: the brief makes the backup pin
      // mandatory from day one. Asserts vanish in release, so this must be a
      // throw - swap it back to an assert and this test still passes under
      // `dart test`, which is why the comment in the source names the reason.
      expect(
        () => CertificatePinning(
          pins: <String>[twoPins.first],
          enforcement: PinEnforcement.enforce,
        ),
        throwsArgumentError,
      );
    });

    test('accepts a pin set that carries a backup pin', () {
      expect(
        () => CertificatePinning(pins: twoPins, enforcement: PinEnforcement.report),
        returnsNormally,
      );
    });

    test('refuses enforce mode without our own certificate', () {
      // Enforcing while still trusting the public store is the posture pinning
      // exists to replace. A mode that only looks enforced is worse than an
      // honest report mode, so construction fails instead.
      expect(
        () => CertificatePinning(pins: twoPins, enforcement: PinEnforcement.enforce),
        throwsArgumentError,
      );
    });

    test('accepts enforce mode when our certificate is supplied', () {
      expect(
        () => CertificatePinning(
          pins: twoPins,
          enforcement: PinEnforcement.enforce,
          trustedCertificate: <int>[1, 2, 3],
        ),
        returnsNormally,
      );
    });

    test('rejects a digest that is not 32 bytes', () {
      expect(
        () => CertificatePinning(
          pins: <String>[base64.encode(List<int>.filled(16, 1)), twoPins.first],
          enforcement: PinEnforcement.enforce,
        ),
        throwsArgumentError,
      );
    });
  });

  group('verifyPeer', () {
    final List<String> twoPins = <String>[
      base64.encode(List<int>.filled(32, 1)),
      base64.encode(List<int>.filled(32, 2)),
    ];

    test('report mode reports a mismatch and lets the connection stand', () {
      String? observed;
      final CertificatePinning pinning = CertificatePinning(
        pins: twoPins,
        enforcement: PinEnforcement.report,
        onMismatch: (String pin) => observed = pin,
      );

      // A null certificate is the strongest mismatch there is.
      expect(() => pinning.verifyPeer(null), returnsNormally);
      expect(observed, '<none>');
    });

    test('enforce mode refuses a mismatch', () {
      final CertificatePinning pinning = CertificatePinning(
        pins: twoPins,
        enforcement: PinEnforcement.enforce,
        trustedCertificate: <int>[1, 2, 3],
      );

      expect(() => pinning.verifyPeer(null), throwsA(isA<TlsException>()));
    });

    test('report mode still calls onMismatch, or the pre-cutover period is blind', () {
      // The whole point of shipping report before the hosting cutover is early
      // warning. Drop the callback and this fails.
      int calls = 0;
      final CertificatePinning pinning = CertificatePinning(
        pins: twoPins,
        enforcement: PinEnforcement.report,
        onMismatch: (String _) => calls++,
      );

      pinning.verifyPeer(null);
      pinning.verifyPeer(null);

      expect(calls, 2);
    });
  });
}
