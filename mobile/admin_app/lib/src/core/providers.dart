import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';

// ── Environment configuration ─────────────────────────────────────────────

final apiBaseUrlProvider = Provider<String>(
  (ref) => const String.fromEnvironment(
    'GP_API_BASE_URL',
    defaultValue: 'https://api.staging.genesisprestige.co.ke/v1',
  ),
);

final pinnedFingerprintsProvider = Provider<List<String>>(
  (ref) => const String.fromEnvironment('GP_PINNED_CERTS', defaultValue: '')
      .split(',')
      .where((s) => s.isNotEmpty)
      .toList(),
);

// ── Infrastructure providers ──────────────────────────────────────────────

final tokenStorageProvider = Provider<TokenStorage>(
  (ref) => TokenStorage(),
);

final certificatePinningProvider = Provider<CertificatePinning>((ref) {
  final fingerprints = ref.watch(pinnedFingerprintsProvider);
  const isRelease = bool.fromEnvironment('dart.vm.product');
  return CertificatePinning(
    pinnedSha256Fingerprints: fingerprints,
    bypassForTesting: !isRelease && fingerprints.isEmpty,
  );
});

final gpHttpClientProvider = Provider<GpHttpClient>((ref) {
  final baseUrl = ref.watch(apiBaseUrlProvider);
  final storage = ref.watch(tokenStorageProvider);
  final pinning = ref.watch(certificatePinningProvider);
  return GpHttpClient(
    baseUrl: baseUrl,
    tokenStorage: storage,
    pinning: pinning,
    onTokensRefreshed: (access, refresh) async {
      await storage.saveTokens(accessToken: access, refreshToken: refresh);
    },
  );
});

final authApiProvider = Provider<AuthApi>(
  (ref) => AuthApi(ref.watch(gpHttpClientProvider)),
);

final membersApiProvider = Provider<MembersApi>(
  (ref) => MembersApi(ref.watch(gpHttpClientProvider)),
);

// ── RBAC permissions ──────────────────────────────────────────────────────

/// Cached permissions for the current admin user.
final permissionsProvider =
    StateProvider<List<String>>((ref) => const []);
