/// What the home hero shows, and the seam the read endpoints will fill.
///
/// [homeSummaryProvider] returns null today, and that is the truthful state
/// rather than a placeholder: no merged endpoint returns a member their own
/// figures. `GET /member/me` is on !7 and the per-account breakdown needs
/// #50, so there is nothing to show and the hero says so.
///
/// The seam exists now, unimplemented, for one reason. When the read surface
/// lands, the work is a repository that produces this type and a provider
/// override that points at it — the screen, the hero, the concealment and
/// the goldens all keep working untouched. Wiring it up afterwards would
/// instead mean editing the screen under time pressure, which is when
/// layouts acquire their special cases.
///
/// Every figure is a server rendered decimal string. There is no constructor
/// taking a number, here or in [GpMoney], so gate 1.1 is enforced by the
/// types rather than by remembering.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:meta/meta.dart';

@immutable
class HomeSummary {
  const HomeSummary({
    required this.totalSavings,
    this.shares,
    this.loanBalance,
  });

  /// The one figure the hero leads with.
  final String totalSavings;

  /// The secondary figures. Nullable independently of [totalSavings]: !7 and
  /// #50 are separate merges, so a build can plausibly know a total while
  /// still not knowing its breakdown, and the hero renders a dash for a
  /// figure it does not have rather than a zero it cannot vouch for.
  final String? shares;
  final String? loanBalance;
}

/// The member's own figures, or null when no endpoint provides them.
///
/// Overridden in tests to render the populated design; overridden by a real
/// repository once !7 merges. Never given fake data in `lib/` — a screen
/// faked against a mock and ticked is the one thing rule 13 forbids outright.
final Provider<HomeSummary?> homeSummaryProvider =
    Provider<HomeSummary?>((Ref ref) => null);
