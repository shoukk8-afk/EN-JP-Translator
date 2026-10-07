import base64
import hashlib
import hmac
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

os.environ.update(DEEPL_API_KEY='test:fx', LINE_CHANNEL_SECRET='test-secret', LINE_CHANNEL_ACCESS_TOKEN='test-token')
import main
from fastapi.testclient import TestClient
from linebot.v3.webhooks import MessageEvent


def event(event_id='evt-1', text='Hello'):
    return MessageEvent.from_dict({
        'type': 'message', 'mode': 'active', 'timestamp': 0,
        'webhookEventId': event_id, 'deliveryContext': {'isRedelivery': False},
        'replyToken': 'test-reply', 'source': {'type': 'user', 'userId': 'test-user'},
        'message': {'type': 'text', 'id': event_id, 'text': text, 'quoteToken': 'test-quote'},
    })


def webhook(client, events):
    body = json.dumps({'destination': 'test', 'events': [e.to_dict() for e in events]})
    signature = base64.b64encode(hmac.new(b'test-secret', body.encode(), hashlib.sha256).digest()).decode()
    return client.post('/callback', content=body, headers={'X-Line-Signature': signature})


class BotTests(unittest.TestCase):
    def setUp(self):
        main._completed.clear()
        main._inflight.clear()

    @patch('main.deepl_translate', return_value={'text': 'Thank you', 'source_language': 'JA'})
    def test_kana_uses_one_call(self, translate):
        self.assertEqual(main.translate_message('ありがとう'), '🇺🇸 Thank you')
        translate.assert_called_once_with('ありがとう', 'EN', 'JA')

    @patch('main.deepl_translate', return_value={'text': 'こんにちは', 'source_language': 'EN'})
    def test_english(self, translate):
        self.assertEqual(main.translate_message('Hello'), '🇯🇵 こんにちは')
        translate.assert_called_once_with('Hello', 'JA')

    @patch('main.deepl_translate')
    def test_kanji_japanese(self, translate):
        translate.side_effect = [{'text': '了解', 'source_language': 'JA'}, {'text': 'Understood', 'source_language': 'JA'}]
        self.assertEqual(main.translate_message('了解'), '🇺🇸 Understood')
        self.assertEqual(translate.call_args.args, ('了解', 'EN', 'JA'))

    @patch('main.deepl_translate')
    def test_other_language_preserved(self, translate):
        translate.side_effect = [{'text': 'ありがとう', 'source_language': 'ZH'}, {'text': 'Thank you', 'source_language': 'ZH'}]
        self.assertEqual(main.translate_message('谢谢'), '🇯🇵 ありがとう\n\n🇺🇸 Thank you')
        self.assertEqual(translate.call_args.args, ('谢谢', 'EN', 'ZH'))

    @patch('main.deepl_translate', return_value={'text': 'Yes', 'source_language': 'JA'})
    def test_explicit_direction(self, translate):
        self.assertEqual(main.translate_message('/en 了解'), '🇺🇸 Yes')
        translate.assert_called_once_with('了解', 'EN')

    @patch('main.time.sleep')
    @patch('main.requests.post')
    def test_retry_rate_limit(self, post, sleep):
        good = MagicMock(status_code=200)
        good.json.return_value = {'translations': [{'text': 'Hello', 'detected_source_language': 'JA'}]}
        post.side_effect = [MagicMock(status_code=429), good]
        self.assertEqual(main.deepl_translate('やあ', 'EN')['text'], 'Hello')
        self.assertEqual(post.call_count, 2)

    @patch('main.time.sleep')
    @patch('main.requests.post', side_effect=main.requests.Timeout)
    def test_timeout_bounded(self, post, sleep):
        with self.assertRaises(main.TranslationError):
            main.deepl_translate('Hello', 'JA')
        self.assertEqual(post.call_count, 2)

    @patch('main.requests.post', return_value=MagicMock(status_code=456))
    def test_quota_not_retried(self, post):
        with self.assertRaises(main.TranslationError) as caught:
            main.deepl_translate('Hello', 'JA')
        self.assertEqual(caught.exception.status, 456)
        self.assertEqual(post.call_count, 1)

    @patch('main.requests.post')
    def test_malformed_response(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'translations': []}
        with self.assertRaises(main.TranslationError):
            main.deepl_translate('Hello', 'JA')

    def test_long_emoji_reply(self):
        text = '🇯🇵 ' + '😀' * 6000
        chunks = main.split_reply(text)
        self.assertEqual(''.join(chunks), text)
        self.assertLessEqual(len(chunks), 5)
        self.assertTrue(all(len(c.encode('utf-16-le')) // 2 <= 5000 for c in chunks))

    def test_reply_over_five_messages(self):
        self.assertEqual(len(main.split_reply('a' * 25001)), 1)
        self.assertIn('Please split', main.split_reply('a' * 25001)[0])

    @patch('main.MessagingApi')
    @patch('main.translate_message', return_value='🇯🇵 こんにちは')
    def test_duplicate_event_replied_once(self, translate, api):
        with TestClient(main.app) as client:
            self.assertEqual(webhook(client, [event()]).status_code, 200)
            self.assertEqual(webhook(client, [event()]).status_code, 200)
        self.assertEqual(api.return_value.reply_message.call_count, 1)
        self.assertEqual(translate.call_count, 1)

    @patch('main.MessagingApi')
    @patch('main.translate_message', side_effect=main.TranslationError(456))
    def test_translation_failure_has_visible_reply(self, translate, api):
        main.handle_message(event())
        request = api.return_value.reply_message.call_args.args[0]
        self.assertIn('quota exceeded', request.messages[0].text)

    @patch('main.MessagingApi')
    @patch('main.translate_message', return_value='こんにちは')
    def test_failed_reply_can_be_redelivered(self, translate, api):
        api.return_value.reply_message.side_effect = [RuntimeError('failed'), None]
        with TestClient(main.app) as client:
            self.assertEqual(webhook(client, [event()]).status_code, 500)
            self.assertEqual(webhook(client, [event()]).status_code, 200)
        self.assertEqual(api.return_value.reply_message.call_count, 2)

    def test_signature_and_verify_request(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.post('/callback', json={'events': []}).status_code, 400)
            self.assertEqual(client.post('/callback', content='{}', headers={'X-Line-Signature': 'bad'}).status_code, 400)
            self.assertEqual(webhook(client, []).status_code, 200)

    @patch('main.MessagingApi')
    @patch('main.translate_message')
    def test_numbers_and_emoji_skipped(self, translate, api):
        main.handle_message(event(text='123 😀'))
        translate.assert_not_called()
        api.assert_not_called()

    @patch('main.ApiClient.call_api', return_value=None)
    @patch('main.translate_message', return_value='こんにちは')
    def test_real_sdk_accepts_timeout_and_payload(self, translate, call_api):
        main.handle_message(event())
        call_api.assert_called_once()
        self.assertEqual(call_api.call_args.kwargs['_request_timeout'], (3, 7))

    def test_batch_runs_concurrently(self):
        barrier = threading.Barrier(2)
        def handle(e):
            barrier.wait(timeout=3)
        with patch('main.handle_message', side_effect=handle):
            with TestClient(main.app) as client:
                self.assertEqual(webhook(client, [event('1'), event('2')]).status_code, 200)

    @patch('main.MessagingApi')
    @patch('main.translate_message', return_value='こんにちは')
    def test_one_batch_failure_does_not_abort_others(self, translate, api):
        api.return_value.reply_message.side_effect = [RuntimeError('failed'), None]
        with TestClient(main.app) as client:
            self.assertEqual(webhook(client, [event('1'), event('2')]).status_code, 500)
        self.assertEqual(api.return_value.reply_message.call_count, 2)
        self.assertEqual(len(main._completed), 1)


if __name__ == '__main__':
    unittest.main()
