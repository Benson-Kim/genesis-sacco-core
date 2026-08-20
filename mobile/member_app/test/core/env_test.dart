/// Flavor and capability-tier guards.
///
/// #43's binding rule, made falsifiable: a capability may only be true when
/// the endpoint behind it is MERGED. Flip one of these to true while its
/// backend work item is still open and this suite fails — which is the point.
/// The register is updated in the same MR that consumes the newly merged
/// surface, never ahead of it.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';
import 'package:member_app/src/core/env.dart';

void main() {
  group('Capabilities.asMergedToday', () {
    const Capabilities merged = Capabilities.asMergedToday;

    test('every read capability is false while the !7 stack is unmerged', () {
      // develop carries POST /member/auth/* and the guarantee consent/release
      // ACTS. It carries no member-audience GET at all.
      expect(merged.balances, isFalse, reason: 'GET /member/me is on !7');
      expect(merged.transactions, isFalse,
          reason: 'GET /member/transactions is on !7');
      expect(merged.statement, isFalse,
          reason: 'GET /member/statement is on !7');
      expect(merged.loans, isFalse, reason: 'GET /member/loans is on !7');
    });

    test('the guarantor inbox is false: the ACTS exist, the LIST does not', () {
      // The subtle one, and the reason #41 was filed. A member can consent to
      // a guarantee only if the app already knows its id; no merged route
      // reveals a member their own pledges.
      expect(merged.guaranteeInbox, isFalse,
          reason: 'GET /member/guarantees is on !31 (#41)');
    });

    test('every money-movement capability is false', () {
      expect(merged.deposits, isFalse,
          reason: 'payment intents are #7, ADR only');
      expect(merged.loanApplication, isFalse,
          reason: 'application + quote is #51');
    });

    test('notifications are false: the outbox is not a read model', () {
      expect(merged.notifications, isFalse,
          reason: 'ADR-0011 read model (#45) is undesigned');
    });
  });

  group('Flavor.dev', () {
    test('speaks https only', () {
      expect(Flavor.dev.baseUrl.scheme, 'https');
    });

    test('carries a pin set with a backup pin', () {
      expect(Flavor.dev.pinSet.length, greaterThanOrEqualTo(2));
      expect(Flavor.dev.pinSet.toSet().length, Flavor.dev.pinSet.length,
          reason: 'two copies of one pin is one pin');
    });

    test('ships report mode until the #11 hosting cutover', () {
      // Enforcing the placeholder pins below would refuse every connection;
      // enforcing against the current shared host would pin a key the fleet
      // never meets in production. Both are why #42 Option 1 adopts own-key
      // CUSTODY now and defers ENFORCEMENT to the release after cutover.
      expect(Flavor.dev.pinEnforcement, PinEnforcement.report);
    });

    test('bakes a tenant id, so nothing has to ask the member for one', () {
      expect(Flavor.dev.tenantId, isNotEmpty);
    });

    test('logs the member out on inactivity regardless of token lifetime', () {
      expect(Flavor.dev.inactivityTimeout,
          lessThanOrEqualTo(const Duration(minutes: 15)));
    });
  });
}
