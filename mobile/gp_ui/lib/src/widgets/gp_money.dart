/// Rendering money, and the two rules that govern it.
///
/// Rule one, no arithmetic ever (gate 1.1). [GpMoney] takes a `String`
/// and there is no constructor that takes a number. Values arrive as
/// server rendered decimal strings and leave as text. A widget that accepted
/// a `double` would be an invitation to compute a total in the UI, and the
/// first person to accept it would be right about the pixels and wrong about
/// the ledger.
///
/// Rule two, the mask must not leak magnitude. Hiding balances is a
/// shoulder surfing control, so a mask sized to the digits it replaces gives
/// away exactly what it was asked to conceal: a reader who sees seven dots
/// knows the figure is in the hundreds of thousands. The mask here is a fixed
/// width regardless of what is behind it.
library;

import 'package:flutter/material.dart';

import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// Digit grouping, done as string surgery and never as arithmetic.
///
/// The house rule is that money is rendered verbatim, and the reason is sound:
/// a client that reformats a monetary string is one bad regular expression
/// away from corrupting it. But `KES 248500.00` is genuinely harder to read
/// than `KES 248,500.00`, and unreadable is its own kind of wrong.
///
/// So the compromise is narrow and falsifiable. Grouping applies ONLY to
/// input matching `^-?\d+\.\d{2}$` exactly. Anything else, including anything
/// already grouped, anything with a currency symbol, and anything unexpected,
/// is returned untouched. The separators are inserted into the integer digits
/// by position. Nothing is parsed into a number and nothing is computed, so
/// there is no precision to lose. The test that keeps this honest strips the
/// separators back out and asserts byte equality with the input.
@visibleForTesting
final RegExp plainDecimal = RegExp(r'^-?\d+\.\d{2}$');

/// Insert thousands separators. Total: any input it does not recognise is
/// returned as it arrived.
@visibleForTesting
String groupDigits(String value) {
  if (!plainDecimal.hasMatch(value)) {
    return value;
  }
  final bool negative = value.startsWith('-');
  final String unsigned = negative ? value.substring(1) : value;
  final int point = unsigned.indexOf('.');
  final String whole = unsigned.substring(0, point);
  final String rest = unsigned.substring(point);

  final StringBuffer grouped = StringBuffer();
  for (int i = 0; i < whole.length; i++) {
    if (i > 0 && (whole.length - i) % 3 == 0) {
      grouped.write(',');
    }
    grouped.write(whole[i]);
  }
  return '${negative ? '-' : ''}$grouped$rest';
}

/// How prominent a monetary figure is.
enum GpMoneySize {
  /// The one figure on the home screen, on the navy hero.
  hero,

  /// A card headline.
  large,

  /// A list row or a secondary tile.
  medium,
}

/// A monetary figure with its currency, in tabular figures.
class GpMoney extends StatelessWidget {
  const GpMoney(
    this.value, {
    super.key,
    this.size = GpMoneySize.medium,
    this.currency = 'KES',
    this.color,
    this.hidden = false,
  });

  /// The server rendered decimal string. Never a number: see the library doc.
  final String value;

  final GpMoneySize size;
  final String currency;

  /// Overrides the scale's own colour, for figures on a coloured ground.
  final Color? color;

  /// Replaces the figure with a fixed width mask, leaving the currency
  /// visible so the row keeps its shape and its meaning.
  final bool hidden;

  /// Wide enough to read as a concealed value, and identical for every
  /// figure so that nothing about the value behind it can be inferred.
  static const String mask = '••••••';

  TextStyle get _figureStyle {
    switch (size) {
      case GpMoneySize.hero:
        return GpTypography.moneyHero;
      case GpMoneySize.large:
        return GpTypography.moneyLarge;
      case GpMoneySize.medium:
        return GpTypography.moneyMedium;
    }
  }

  @override
  Widget build(BuildContext context) {
    final TextStyle figure = color == null
        ? _figureStyle
        : _figureStyle.copyWith(color: color);
    final Color prefixColor =
        (color ?? figure.color ?? GpPalette.ink).withValues(alpha: 0.72);

    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.baseline,
      textBaseline: TextBaseline.alphabetic,
      children: <Widget>[
        Text(
          currency,
          style: GpTypography.currencyPrefix.copyWith(color: prefixColor),
        ),
        const SizedBox(width: 6),
        Text(
          hidden ? mask : groupDigits(value),
          style: figure,
          // A concealed figure still announces itself as concealed, rather
          // than reading six bullet characters aloud.
          semanticsLabel: hidden ? 'hidden' : null,
        ),
      ],
    );
  }
}
