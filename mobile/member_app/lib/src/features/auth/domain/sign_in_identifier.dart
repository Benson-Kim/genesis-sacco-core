/// The sign-in identifier (#35): an email address or a Kenya mobile number.
///
/// **This file validates. It does NOT normalize**, and the distinction is the
/// whole design.
///
/// `genesis.domain.members.normalize_kenya_msisdn` is the single normalizer,
/// and the backend says so in as many words: "never a second normalizer".
/// Every accepted spelling of a number maps to one E.164 string THERE, and
/// stored credentials are E.164 by the 0042 backfill. A client that helpfully
/// rewrote `0712345678` into `+254712345678` before sending would be a second
/// normalizer — one that nobody tests against the first, and that silently
/// starts resolving different members the day the two disagree. So the value
/// travels to the server exactly as it was typed, minus surrounding
/// whitespace, which is the one transformation the server also performs.
///
/// What is checked locally is SHAPE, and only shape. That is not an existence
/// oracle and does not weaken gate 1.6: the client is reporting on the string
/// the member just typed, which the member already knows, and it learns
/// nothing from the server to do it.
///
/// Shape checking earns its place because of how the server classifies. From
/// `resolve_signin_identifier`: anything the msisdn rule rejects "takes the
/// email path byte-identically — including malformed phone-ish strings". That
/// is the right server behaviour (no existence oracle) and a miserable client
/// experience: type `07123` and the server dutifully looks for an *email*
/// called `07123`, finds nothing, and answers `{"status":"sent"}` like always.
/// The member waits for a code that was never going to arrive. Catching the
/// typo here is the only place it can be caught at all.
library;

/// The four spellings the server accepts, mirrored from
/// `genesis/domain/members.py`. Mirrored, not re-derived: if the maintainers
/// add a prefix there, this pair changes in the same MR.
final RegExp _kenyaMsisdnLocal = RegExp(r'^0[17]\d{8}$');
final RegExp _kenyaMsisdnE164 = RegExp(r'^\+254[17]\d{8}$');

/// Phone-ISH, not phone-VALID. A value that opens like a number is one the
/// member meant as a number, so it is held to the number rule rather than
/// being quietly demoted to the email path.
final RegExp _looksNumeric = RegExp(r'^[+0-9][0-9 +()-]*$');

/// `email`/`identifier` are `Field(min_length=3, max_length=254)` on
/// `OtpIdentifierBody`. Enforced here so a value that could only ever be a 422
/// never costs a round trip.
const int _minLength = 3;
const int _maxLength = 254;

/// Why a typed identifier cannot be sent.
///
/// Deliberately a small closed set: each value is a statement about the text
/// in the field, never about the server's records.
enum IdentifierProblem {
  /// Nothing typed yet.
  empty,

  /// Under `min_length` or over `max_length`.
  length,

  /// Opened like a phone number but is not one of the four accepted
  /// spellings — the case the server would silently treat as an email.
  malformedPhone,

  /// Not phone-ish, and not shaped like an email address either.
  malformedEmail,
}

/// How the identifier will be classified server-side. Carried so the UI can
/// say "we sent a code to your phone" rather than something vaguer, and for
/// nothing else — the client never acts on this.
enum IdentifierKind { phone, email }

/// A validated identifier, holding the value in the form it will be SENT.
class SignInIdentifier {
  const SignInIdentifier._(this.value, this.kind);

  /// Exactly what goes on the wire: trimmed, otherwise untouched.
  final String value;
  final IdentifierKind kind;

  /// Validate [raw], or return the reason it cannot be sent.
  ///
  /// Returns a record rather than throwing: an invalid identifier is the
  /// normal state of a text field halfway through being typed, not an
  /// exceptional condition.
  static ({SignInIdentifier? identifier, IdentifierProblem? problem}) parse(
    String raw,
  ) {
    final String value = raw.trim();
    if (value.isEmpty) {
      return (identifier: null, problem: IdentifierProblem.empty);
    }
    if (value.length < _minLength || value.length > _maxLength) {
      return (identifier: null, problem: IdentifierProblem.length);
    }
    if (_looksNumeric.hasMatch(value)) {
      final bool accepted = _kenyaMsisdnLocal.hasMatch(value) ||
          _kenyaMsisdnE164.hasMatch(value);
      return accepted
          ? (
              identifier: SignInIdentifier._(value, IdentifierKind.phone),
              problem: null
            )
          : (identifier: null, problem: IdentifierProblem.malformedPhone);
    }
    if (!_isEmailShaped(value)) {
      return (identifier: null, problem: IdentifierProblem.malformedEmail);
    }
    return (
      identifier: SignInIdentifier._(value, IdentifierKind.email),
      problem: null
    );
  }

  /// Structural only: one `@`, something either side, a dot in the domain.
  ///
  /// No RFC 5322 attempt, and no attempt at one. The server does not validate
  /// email structure at all — an unmatched address simply resolves no
  /// credential — so anything stricter here would reject addresses the server
  /// would have accepted, which is a client inventing policy.
  static bool _isEmailShaped(String value) {
    final int at = value.indexOf('@');
    if (at <= 0 || at != value.lastIndexOf('@') || at == value.length - 1) {
      return false;
    }
    final String domain = value.substring(at + 1);
    final int dot = domain.indexOf('.');
    return dot > 0 && dot < domain.length - 1;
  }
}
