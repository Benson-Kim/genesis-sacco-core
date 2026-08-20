/// The Idempotency-Key stability/rotation contract, falsifiable.
///
/// `fresh` is injected so the oracle is exact: keys are a counted sequence
/// ('k1', 'k2', ...) rather than real UUIDs, and the assertions are about WHEN
/// a new one is drawn, which is the whole contract.
///
/// Scaffold defect D7: the P16 client generated no keys at all, so FM-G
/// (double-tap creates two effects) had no client-side guard.
library;

import 'package:flutter_test/flutter_test.dart';
import 'package:gp_api_client/gp_api_client.dart';

void main() {
  late int drawn;
  String fresh() => 'k${++drawn}';

  setUp(() => drawn = 0);

  group('IdempotencyKeySlot', () {
    test('retrying the identical body REUSES the key (the server replays)', () {
      final IdempotencyKeySlot slot = IdempotencyKeySlot();

      final String first = slot.keyFor('{"version":3}', fresh: fresh);
      final String retry = slot.keyFor('{"version":3}', fresh: fresh);

      expect(first, 'k1');
      expect(retry, 'k1');
      expect(drawn, 1, reason: 'a retry must not draw a second key');
    });

    test('changing the body is a NEW intent and rotates the key', () {
      final IdempotencyKeySlot slot = IdempotencyKeySlot();

      final String first = slot.keyFor('{"version":3}', fresh: fresh);
      final String second = slot.keyFor('{"version":4}', fresh: fresh);

      expect(first, 'k1');
      expect(second, 'k2');
    });

    test('rotating back to an earlier body still draws a fresh key', () {
      // The slot holds one intent, not a history: returning to a previous
      // body is a new logical submission, so replaying the old response would
      // be wrong.
      final IdempotencyKeySlot slot = IdempotencyKeySlot();

      slot.keyFor('{"version":3}', fresh: fresh);
      slot.keyFor('{"version":4}', fresh: fresh);
      final String back = slot.keyFor('{"version":3}', fresh: fresh);

      expect(back, 'k3');
    });

    test('reset makes the next use a new intent', () {
      final IdempotencyKeySlot slot = IdempotencyKeySlot();

      slot.keyFor('{"version":3}', fresh: fresh);
      slot.reset();
      final String afterReset = slot.keyFor('{"version":3}', fresh: fresh);

      expect(afterReset, 'k2');
    });

    test('separate slots never share a key', () {
      final IdempotencyKeySlot consent = IdempotencyKeySlot();
      final IdempotencyKeySlot release = IdempotencyKeySlot();

      expect(consent.keyFor('{"version":3}', fresh: fresh), 'k1');
      expect(release.keyFor('{"version":3}', fresh: fresh), 'k2');
    });
  });

  group('newIdempotencyKey', () {
    test('draws distinct v4 UUIDs', () {
      final String a = newIdempotencyKey();
      final String b = newIdempotencyKey();

      expect(a, isNot(b));
      expect(a.length, 36);
    });
  });
}
