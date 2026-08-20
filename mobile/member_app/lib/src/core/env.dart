/// Build-time flavor configuration — one white-label app per SACCO.
///
/// Owner sign-off, 2026-08-20: the tenant is baked at build time. There is no
/// runtime tenant switcher, and this is a security decision, not a packaging
/// preference: a switcher makes the tenant a value the CLIENT asserts, which
/// is the client side half of the principal confusion class the whole
/// `/member` surface is designed to exclude.
///
/// Everything here is `const` and read from `--dart-define` at compile time,
/// so a flavor is configuration rather than surgery and nothing below can be
/// changed by a running app.
library;

import 'package:gp_api_client/gp_api_client.dart';

/// Which capabilities this build is allowed to show.
///
/// #43's binding rule: no screen ships against an endpoint that is not merged,
/// and a Favorites tile must never launch an unbuilt flow. Tier state is a
/// build-time map keyed to merged backend surface — deliberately NOT a runtime
/// feature probe, because a probe would let a server enable a screen the app
/// has no code for.
class Capabilities {
  const Capabilities({
    required this.guaranteeInbox,
    required this.balances,
    required this.transactions,
    required this.statement,
    required this.loans,
    required this.deposits,
    required this.loanApplication,
    required this.notifications,
  });

  /// MR-2, gated on #41 (`GET /member/guarantees`) merging.
  final bool guaranteeInbox;

  /// MR-3, gated on !7. Per-account breakdown additionally needs #50.
  final bool balances;
  final bool transactions;
  final bool statement;
  final bool loans;

  /// MR-5, gated on #7 (P19 payment intents).
  final bool deposits;

  /// MR-4, gated on #51.
  final bool loanApplication;

  /// Gated on ADR-0011's read model (#45).
  final bool notifications;

  /// What is merged on `develop` as of this build: the OTP session and the
  /// guarantor consent/release ACTS. Every read surface is still in the !7
  /// queue, so every read capability is false — honestly, not aspirationally.
  static const Capabilities asMergedToday = Capabilities(
    guaranteeInbox: false,
    balances: false,
    transactions: false,
    statement: false,
    loans: false,
    deposits: false,
    loanApplication: false,
    notifications: false,
  );
}

/// One SACCO's build.
class Flavor {
  const Flavor({
    required this.name,
    required this.baseUrl,
    required this.tenantId,
    required this.pinSet,
    required this.pinEnforcement,
    required this.capabilities,
    required this.inactivityTimeout,
  });

  final String name;
  final Uri baseUrl;

  /// Sent as `x-tenant-id` on every request, including the pre-auth OTP routes
  /// that must resolve a tenant before any credential exists.
  final String tenantId;

  /// SPKI pins (#42 Option 1): our own key, plus an offline backup key.
  final List<String> pinSet;

  /// Report until the #11 hosting cutover, then enforce — see
  /// [PinEnforcement]. The value is compiled in; there is no toggle.
  final PinEnforcement pinEnforcement;

  final Capabilities capabilities;

  /// #43 T0: the session ends on inactivity regardless of token lifetime.
  final Duration inactivityTimeout;

  /// The dev flavor. Environments are dev-only for now (owner sign-off), and
  /// `core/env` stays honest so adding staging/prod is configuration.
  ///
  /// The pin values below are placeholders in the literal sense: they are two
  /// distinct, well-formed SHA-256 digests so the shape is exercised, and they
  /// match no real key. They are replaced by the real own-key SPKI pins when
  /// #42's keypair is generated — which is why this flavor ships
  /// [PinEnforcement.report]: enforcing placeholder pins would refuse every
  /// connection.
  static Flavor dev = Flavor(
    name: 'dev',
    baseUrl: Uri.parse(
      String.fromEnvironment('GP_BASE_URL', defaultValue: 'https://dev.invalid'),
    ),
    tenantId: const String.fromEnvironment('GP_TENANT_ID', defaultValue: 'dev-tenant'),
    pinSet: const <String>[
      'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
      'AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=',
    ],
    pinEnforcement: PinEnforcement.report,
    capabilities: Capabilities.asMergedToday,
    inactivityTimeout: const Duration(minutes: 5),
  );
}
