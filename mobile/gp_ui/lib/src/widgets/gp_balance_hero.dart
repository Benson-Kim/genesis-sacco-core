/// The navy hero at the top of the home screen.
///
/// This is the mobile app's equivalent of the console's dashboard row: the
/// one surface the eye lands on first. It reuses the prototype's login gate
/// treatment — the `linear-gradient(140deg, navy, navyMid)` ground and the
/// 20px corner — because that gradient is the only place the prototype lets
/// the brand colour fill a whole surface, and the home screen deserves it
/// more than a login screen does.
///
/// # It has an honest empty state, and that is the point
///
/// Every read capability is currently false: no merged endpoint returns a
/// member their own figures. A hero that showed plausible numbers anyway
/// would be the exact failure rule 13 forbids — a screen faked against a mock
/// and ticked. So [GpBalanceHero] takes a nullable value and, when it is
/// null, says so in plain words instead. The layout is identical either way,
/// so what is being reviewed now is what will ship later.
library;

import 'package:flutter/material.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';
import 'gp_money.dart';

/// One of the smaller figures under the rule.
@immutable
class GpHeroFigure {
  const GpHeroFigure({required this.label, required this.value});

  final String label;

  /// Null when the figure is not available yet, exactly as in the hero.
  final String? value;
}

class GpBalanceHero extends StatelessWidget {
  const GpBalanceHero({
    required this.title,
    required this.value,
    super.key,
    this.figures = const <GpHeroFigure>[],
    this.hidden = false,
    this.onToggleHidden,
    this.unavailableMessage,
    this.footnote,
  });

  /// "Total savings".
  final String title;

  /// The server rendered decimal string, or null when no merged endpoint
  /// provides it. Never a number: see [GpMoney].
  final String? value;

  /// The secondary figures under the rule. Two reads well on a phone; three
  /// is the practical limit before they stop being legible.
  final List<GpHeroFigure> figures;

  final bool hidden;

  /// Null hides the eye control entirely, which is correct when there is no
  /// figure to conceal.
  final VoidCallback? onToggleHidden;

  /// Shown in place of the figure when [value] is null.
  final String? unavailableMessage;

  /// A qualifier under the figure — the staleness marker for a cached read,
  /// for instance.
  final Widget? footnote;

  @override
  Widget build(BuildContext context) {
    final String? figure = value;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(GpSpace.xl),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(GpRadius.hero),
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: <Color>[GpPalette.navy, GpPalette.navyMid],
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              Expanded(
                child: Text(
                  title,
                  style: GpTypography.labelSmall.copyWith(
                    color: Colors.white.withValues(alpha: 0.78),
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              if (onToggleHidden != null && figure != null)
                _EyeToggle(hidden: hidden, onPressed: onToggleHidden!),
            ],
          ),
          const SizedBox(height: GpSpace.md),
          if (figure == null)
            Text(
              unavailableMessage ?? 'Not available yet',
              style: GpTypography.bodyMedium.copyWith(
                color: Colors.white.withValues(alpha: 0.82),
                fontWeight: FontWeight.w600,
              ),
            )
          else
            GpMoney(figure, size: GpMoneySize.hero, hidden: hidden),
          if (footnote != null) ...<Widget>[
            const SizedBox(height: GpSpace.sm),
            footnote!,
          ],
          if (figures.isNotEmpty) ...<Widget>[
            const SizedBox(height: GpSpace.lg),
            Divider(
              height: 1,
              thickness: 1,
              color: Colors.white.withValues(alpha: 0.18),
            ),
            const SizedBox(height: GpSpace.lg),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                for (final GpHeroFigure item in figures)
                  Expanded(child: _SecondaryFigure(item: item, hidden: hidden)),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class _SecondaryFigure extends StatelessWidget {
  const _SecondaryFigure({required this.item, required this.hidden});

  final GpHeroFigure item;
  final bool hidden;

  @override
  Widget build(BuildContext context) {
    final String? figure = item.value;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          item.label,
          style: GpTypography.bodySmall.copyWith(
            color: Colors.white.withValues(alpha: 0.7),
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: GpSpace.xs),
        if (figure == null)
          Text(
            '—',
            style: GpTypography.moneyMedium.copyWith(
              color: Colors.white.withValues(alpha: 0.55),
            ),
          )
        else
          GpMoney(
            figure,
            size: GpMoneySize.medium,
            color: Colors.white,
            hidden: hidden,
          ),
      ],
    );
  }
}

class _EyeToggle extends StatelessWidget {
  const _EyeToggle({required this.hidden, required this.onPressed});

  final bool hidden;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return IconButton(
      onPressed: onPressed,
      visualDensity: VisualDensity.compact,
      // The label states the ACTION, not the state, because that is what a
      // screen reader user is choosing to do.
      tooltip: hidden ? 'Show figures' : 'Hide figures',
      icon: Icon(
        hidden ? Icons.visibility_off_rounded : Icons.visibility_rounded,
        color: Colors.white.withValues(alpha: 0.85),
        size: 20,
      ),
    );
  }
}
