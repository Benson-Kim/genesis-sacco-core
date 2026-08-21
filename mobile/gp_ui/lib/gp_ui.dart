/// Genesis Prestige shared UI package.
///
/// Design tokens and the component vocabulary shared by `member_app` and,
/// later, `admin_app`.
///
/// # Provenance
///
/// The palette is extracted verbatim from `genesis_prestige_app.html`, the
/// canonical prototype, and `palette_test` asserts it still matches the web
/// design system's `tokens.ts` — one brand, checked rather than remembered.
///
/// The COMPONENTS are derived from the same prototype but deliberately not
/// copied from it. The prototype is a desktop staff console: a fixed sidebar,
/// 13px body copy, controls about 35px tall. Its colours, radii and card
/// idiom are the brand and carry over unchanged; its measurements are
/// calibrated for a cursor at desk distance and are retuned here for a thumb
/// at arm's length. Where the two disagree, the reason is recorded at the
/// point of disagreement rather than in a changelog nobody opens.
library gp_ui;

export 'src/tokens/geometry.dart';
export 'src/tokens/palette.dart';
export 'src/tokens/theme.dart';
export 'src/tokens/typography.dart';
export 'src/widgets/gp_action_tile.dart';
export 'src/widgets/gp_balance_hero.dart';
export 'src/widgets/gp_bottom_nav.dart';
export 'src/widgets/gp_brand.dart';
export 'src/widgets/gp_controls.dart';
export 'src/widgets/gp_error_view.dart';
export 'src/widgets/gp_inputs.dart';
export 'src/widgets/gp_loading_view.dart';
export 'src/widgets/gp_money.dart';
export 'src/widgets/gp_states.dart';
export 'src/widgets/gp_stat_card.dart';
export 'src/widgets/gp_surfaces.dart';
