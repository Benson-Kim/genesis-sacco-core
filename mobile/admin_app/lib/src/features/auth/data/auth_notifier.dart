import 'package:flutter_riverpod/flutter_riverpod.dart';
import '../../../core/providers.dart';
import 'auth_repository.dart';

sealed class AuthState {
  const AuthState();
}

class AuthInitial extends AuthState {
  const AuthInitial();
}

class AuthOtpRequested extends AuthState {
  const AuthOtpRequested(this.phone);
  final String phone;
}

class AuthAuthenticated extends AuthState {
  const AuthAuthenticated(this.permissions);
  final List<String> permissions;
}

class AuthLoading extends AuthState {
  const AuthLoading();
}

class AuthError extends AuthState {
  const AuthError(this.category, this.correlationId);
  final String category;
  final String correlationId;
}

class AuthNotifier extends StateNotifier<AuthState> {
  AuthNotifier(this._repo, this._ref) : super(const AuthInitial());

  final AuthRepository _repo;
  final Ref _ref;

  Future<void> checkSession() async {
    state = const AuthLoading();
    final authenticated = await _repo.isAuthenticated();
    state = authenticated
        ? const AuthAuthenticated([])
        : const AuthInitial();
  }

  Future<void> requestOtp(String phone) async {
    state = const AuthLoading();
    try {
      await _repo.requestOtp(phone);
      state = AuthOtpRequested(phone);
    } on Exception catch (e) {
      state = AuthError(e.toString(), '');
    }
  }

  Future<void> verifyOtp(String phone, String otp) async {
    state = const AuthLoading();
    try {
      final permissions = await _repo.verifyOtpAndSave(phone, otp);
      _ref.read(permissionsProvider.notifier).state = permissions;
      state = AuthAuthenticated(permissions);
    } on Exception catch (e) {
      state = AuthError(e.toString(), '');
    }
  }

  Future<void> logout() async {
    await _repo.logout();
    _ref.read(permissionsProvider.notifier).state = [];
    state = const AuthInitial();
  }
}

final authNotifierProvider =
    StateNotifierProvider<AuthNotifier, AuthState>((ref) {
  return AuthNotifier(ref.watch(authRepositoryProvider), ref);
});
