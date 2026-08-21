/// Spacing, radius and elevation — the measurements the prototype encodes in
/// CSS and this package has so far been re-deciding at every call site.
///
/// # What is carried over, and what is retuned
///
/// The prototype (`genesis_prestige_app.html`) is a DESKTOP staff console:
/// a 234px sidebar, 13px body text, 9px button padding. Its colours and its
/// component vocabulary are the brand; its MEASUREMENTS are calibrated for a
/// mouse at desk distance and do not survive the trip to a phone held at arm's
/// length.
///
/// So radii and the card idiom carry over unchanged — they are what makes the
/// two products look like one product — while touch targets, gutters and type
/// sizes are retuned. A 35px-tall button is fine with a cursor and a
/// well-known accessibility failure with a thumb.
library;

/// The spacing scale. Every gap in the app is one of these.
///
/// A 4px base, because the prototype's own values (4, 8, 10, 12, 16, 20, 30)
/// are all multiples of it once 10 rounds to 12 and 30 to 32. Values that are
/// "nearly" a step are the reason interfaces drift.
abstract final class GpSpace {
  static const double xs = 4;
  static const double sm = 8;
  static const double md = 12;
  static const double lg = 16;
  static const double xl = 20;
  static const double xxl = 24;
  static const double xxxl = 32;

  /// Screen gutter. 16 rather than the prototype's 20: on a 360dp phone,
  /// 20 either side spends 11% of the width on nothing.
  static const double gutter = 16;

  /// Inside a card. The prototype's `.pad`, kept — cards need more inner
  /// breathing room than the screen edge does.
  static const double cardPadding = 20;

  /// Minimum touch target (Material's floor, and WCAG 2.5.5's).
  ///
  /// The prototype's `.btn` computes to roughly 35px tall. That is not a
  /// value to carry over; it is a value to leave behind.
  static const double touchTarget = 48;
}

/// Corner radii, all from the prototype.
abstract final class GpRadius {
  /// `.card` — 16.
  static const double card = 16;

  /// `.btn`, `.nav-item`, `.av` — 10 in the prototype, 12 here: the same
  /// shape reads tighter at a larger control size.
  static const double control = 12;

  /// `input` — 9 in the prototype, rounded to the scale.
  static const double field = 10;

  /// `.gatecard` — 20. Reused for the balance hero, which plays the same
  /// role on mobile that the gate card plays on desktop: the one surface
  /// the eye lands on first.
  static const double hero = 20;

  /// `.pill`, `.chip` — fully round.
  static const double pill = 999;
}

/// The one shadow this design uses, and the reason there is only one.
///
/// The prototype is a border-and-fill design: `.card` is a 1px `--line`
/// border on white, with no shadow anywhere except the login gate's
/// `0 30px 90px rgba(0,0,0,.45)`. That restraint is deliberate and worth
/// keeping — stacked shadows are how a flat design becomes a muddy one — so
/// elevation is reserved for surfaces that genuinely float above the page:
/// the sign-in card, sheets, and the bottom navigation bar.
abstract final class GpElevation {
  static const double none = 0;
  static const double raised = 2;
  static const double floating = 12;
}
