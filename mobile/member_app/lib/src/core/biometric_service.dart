import 'package:local_auth/local_auth.dart';

/// Biometric step-up authentication service.
///
/// Used for sensitive actions (loan application, guarantor consent, etc.)
/// per MASTER_PROMPT §2.4 biometric unlock requirement.
class BiometricService {
  BiometricService({LocalAuthentication? auth})
      : _auth = auth ?? LocalAuthentication();

  final LocalAuthentication _auth;

  /// Returns true if the device supports biometrics and has enrolled credentials.
  Future<bool> isAvailable() async {
    final canCheck = await _auth.canCheckBiometrics;
    if (!canCheck) return false;
    final biometrics = await _auth.getAvailableBiometrics();
    return biometrics.isNotEmpty;
  }

  /// Prompts the user for biometric authentication.
  ///
  /// [reason] is shown in the system dialog — must not contain PII (§1.6).
  /// Returns true if authentication succeeded.
  Future<bool> authenticate({
    String reason = 'Confirm your identity to continue',
  }) async {
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          biometricOnly: false, // allow PIN fallback
          stickyAuth: true,
        ),
      );
    } catch (_) {
      // Authentication errors are non-fatal; caller decides how to proceed.
      return false;
    }
  }
}
