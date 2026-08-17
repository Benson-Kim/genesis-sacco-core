import 'package:local_auth/local_auth.dart';

/// Biometric step-up for sensitive admin actions (committee votes, approvals).
/// Mirrors member_app implementation — shared logic will move to gp_ui in P18.
class BiometricService {
  BiometricService({LocalAuthentication? auth})
      : _auth = auth ?? LocalAuthentication();

  final LocalAuthentication _auth;

  Future<bool> isAvailable() async {
    final canCheck = await _auth.canCheckBiometrics;
    if (!canCheck) return false;
    final biometrics = await _auth.getAvailableBiometrics();
    return biometrics.isNotEmpty;
  }

  /// [reason] must not contain PII (MASTER_PROMPT §1.6).
  Future<bool> authenticate({
    String reason = 'Confirm your identity to authorise this action',
  }) async {
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          biometricOnly: false,
          stickyAuth: true,
        ),
      );
    } catch (_) {
      return false;
    }
  }
}
