import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:admin_app/src/features/auth/data/auth_notifier.dart';
import 'package:admin_app/src/features/auth/data/auth_repository.dart';

// ── Fake repository ───────────────────────────────────────────────────────

class _FakeAuthRepository implements AuthRepository {
  bool otpRequested = false;
  bool otpVerified = false;
  bool loggedOut = false;
  bool shouldThrow = false;
  bool _hasTokens = false;
  List<String> _permissions = ['members:view', 'loans:view'];

  @override
  Future<void> requestOtp(String phone) async {
    if (shouldThrow) throw Exception('rate_limited');
    otpRequested = true;
  }

  @override
  Future<List<String>> verifyOtpAndSave(String phone, String otp) async {
    if (shouldThrow) throw Exception('invalid_otp');
    otpVerified = true;
    _hasTokens = true;
    return _permissions;
  }

  @override
  Future<bool> isAuthenticated() async => _hasTokens;

  @override
  Future<void> logout() async {
    loggedOut = true;
    _hasTokens = false;
  }
}

// ── Tests ─────────────────────────────────────────────────────────────────

void main() {
  late _FakeAuthRepository fakeRepo;

  setUp(() {
    fakeRepo = _FakeAuthRepository();
  });

  group('AdminApp AuthNotifier', () {
    ProviderContainer makeContainer() => ProviderContainer(
          overrides: [
            authRepositoryProvider.overrideWithValue(fakeRepo),
          ],
        );

    test('initial state is AuthInitial', () {
      final container = makeContainer();
      addTearDown(container.dispose);
      expect(container.read(authNotifierProvider), isA<AuthInitial>());
    });

    test('requestOtp transitions to AuthOtpRequested', () async {
      final container = makeContainer();
      addTearDown(container.dispose);
      await container
          .read(authNotifierProvider.notifier)
          .requestOtp('+254700000000');
      expect(container.read(authNotifierProvider), isA<AuthOtpRequested>());
      expect(fakeRepo.otpRequested, isTrue);
    });

    test('verifyOtp transitions to AuthAuthenticated with permissions',
        () async {
      final container = makeContainer();
      addTearDown(container.dispose);
      await container
          .read(authNotifierProvider.notifier)
          .verifyOtp('+254700000000', '123456');
      final state = container.read(authNotifierProvider);
      expect(state, isA<AuthAuthenticated>());
      expect(
        (state as AuthAuthenticated).permissions,
        containsAll(['members:view', 'loans:view']),
      );
    });

    test('verifyOtp error transitions to AuthError', () async {
      fakeRepo.shouldThrow = true;
      final container = makeContainer();
      addTearDown(container.dispose);
      await container
          .read(authNotifierProvider.notifier)
          .verifyOtp('+254700000000', '000000');
      expect(container.read(authNotifierProvider), isA<AuthError>());
    });

    test('logout clears tokens and returns to AuthInitial', () async {
      fakeRepo._hasTokens = true;
      final container = makeContainer();
      addTearDown(container.dispose);
      await container.read(authNotifierProvider.notifier).logout();
      expect(container.read(authNotifierProvider), isA<AuthInitial>());
      expect(fakeRepo.loggedOut, isTrue);
    });
  });
}
