/// What a screen needs to know about a guarantee.
///
/// The wire type stops here. `GuaranteeOut` is a generated transport shape,
/// and letting it reach a widget would put `borrower_member_id` and a raw
/// status string one autocomplete away from being rendered. This translates
/// once, in the layer whose job that is.
///
/// # Status is mapped, not passed through
///
/// The server's vocabulary is `pledged`, `active`, `released`, `rejected`.
/// Those are ledger words; a member reading their phone is asking a different
/// question, which is "does this need me". So `pledged` becomes "Awaiting
/// your decision" rather than a lowercase noun nobody outside the SACCO
/// office uses. An unrecognised status is NOT rendered raw — a future backend
/// value would otherwise leak a term the UI has never been designed around.
library;

import 'package:gp_api_client/gp_api_client.dart';
import 'package:meta/meta.dart';

/// Where a guarantee stands, in the terms the member cares about.
enum GuaranteeStanding {
  /// `pledged` — the member has been named and has not yet decided.
  awaitingDecision,

  /// `active` — consented. Binding, and not releasable from this app.
  active,

  /// `released` — the pledge was withdrawn before it became binding.
  withdrawn,

  /// `rejected` — terminal, decided elsewhere.
  rejected,

  /// A status this app has never heard of. Rendered as neutral and
  /// unremarkable rather than as a raw string.
  unknown,
}

@immutable
class GuaranteeView {
  const GuaranteeView({
    required this.id,
    required this.version,
    required this.figure,
    required this.standing,
    required this.reference,
    required this.borrowerReference,
  });

  factory GuaranteeView.of(GuaranteeOut record) => GuaranteeView(
        id: record.id,
        version: record.version,
        figure: record.amount,
        standing: _standingOf(record.status),
        reference: _shortRef(
          record.loanId ?? record.applicationId ?? record.id,
        ),
        borrowerReference: _shortRef(record.borrowerMemberId),
      );

  final String id;

  /// The optimistic lock fence. Sent back with the act; a mismatch is a 409.
  final int version;

  /// The server rendered decimal string. Never a number.
  final String figure;

  final GuaranteeStanding standing;

  /// A short, quotable reference for this request.
  final String reference;

  /// The borrower, as a short reference.
  ///
  /// The merged contract carries `borrower_member_id` and no name, so this is
  /// the most a screen can honestly say about who is being guaranteed. A
  /// full UUID is not information to a member; the first segment is at least
  /// quotable to support. Worth a backend work item, not a client side guess.
  final String borrowerReference;

  String get statusLabel {
    switch (standing) {
      case GuaranteeStanding.awaitingDecision:
        return 'Awaiting your decision';
      case GuaranteeStanding.active:
        return 'Active';
      case GuaranteeStanding.withdrawn:
        return 'Withdrawn';
      case GuaranteeStanding.rejected:
        return 'Not proceeding';
      case GuaranteeStanding.unknown:
        return 'In progress';
    }
  }

  /// Whether this app can still act on it. Both acts require `pledged`; the
  /// server enforces it under the row lock, and the UI agrees rather than
  /// offering controls the server will refuse.
  bool get actionable => standing == GuaranteeStanding.awaitingDecision;

  static GuaranteeStanding _standingOf(String status) {
    switch (status) {
      case 'pledged':
        return GuaranteeStanding.awaitingDecision;
      case 'active':
        return GuaranteeStanding.active;
      case 'released':
        return GuaranteeStanding.withdrawn;
      case 'rejected':
        return GuaranteeStanding.rejected;
      default:
        return GuaranteeStanding.unknown;
    }
  }

  /// The first segment of a UUID: enough for a member to read out to support,
  /// short enough to belong on a phone screen.
  static String _shortRef(String id) {
    final int dash = id.indexOf('-');
    return (dash > 0 ? id.substring(0, dash) : id).toUpperCase();
  }
}
