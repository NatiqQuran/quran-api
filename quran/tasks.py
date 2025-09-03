from celery import shared_task
from quran.models import (
    Mushaf,
    Surah,
    Ayah,
    Word,
    Translation,
    AyahTranslation,
)
from django.contrib.auth import get_user_model
from django.db import transaction
from django.conf import settings
import requests

from core.models import Notification

@shared_task
def import_mushaf_task(quran_data, user_id):
    User = get_user_model()
    user = User.objects.get(id=user_id)
    mushaf_data = quran_data["mushaf"]
    with transaction.atomic():
        mushaf = Mushaf.objects.create(
            creator_id=user.id,
            name=mushaf_data["name"],
            short_name=mushaf_data["short_name"],
            source=mushaf_data["source"]
        )
        surah_objs = []
        for surah_data in quran_data["surahs"]:
            surah_objs.append(Surah(
                creator_id=user.id,
                mushaf=mushaf,
                number=surah_data["number"],
                name=surah_data["name"],
                period=surah_data["period"]
            ))
        Surah.objects.bulk_create(surah_objs)
        surahs_by_number = {s.number: s for s in mushaf.surahs.all()}
        ayah_objs = []
        for surah_data in quran_data["surahs"]:
            surah = surahs_by_number[surah_data["number"]]
            for ayah in surah_data["ayahs"]:
                # Calculate length from words if available
                length = 0
                if "words" in ayah:
                    text = ' '.join(word["text"] for word in ayah["words"])
                    length = len(text)
                
                ayah_objs.append(Ayah(
                    creator_id=user.id,
                    surah=surah,
                    number=ayah["number"],
                    sajdah=ayah["sajdah"],
                    is_bismillah=ayah["is_bismillah"],
                    bismillah_text=ayah["bismillah_text"],
                    length=length,
                ))
        Ayah.objects.bulk_create(ayah_objs)
        ayahs_by_surah_and_number = {(a.surah.number, a.number): a for a in Ayah.objects.filter(surah__mushaf=mushaf)}
        word_objs = []
        for surah_data in quran_data["surahs"]:
            for ayah in surah_data["ayahs"]:
                ayah_obj = ayahs_by_surah_and_number[(surah_data["number"], ayah["number"])]
                for word in ayah["words"]:
                    word_objs.append(Word(ayah=ayah_obj, text=word["text"], creator_id=user.id))
        Word.objects.bulk_create(word_objs)
    # Send notification to user
    Notification.objects.create(
        user=user,
        resource_controller="mushafs",
        resource_action="import",
        resource_uuid=mushaf.uuid,
        status=Notification.STATUS_NOTHING,
        description=f'Mushaf import complete',
        message=f'Mushaf "{mushaf.name}" imported successfully.',
        message_type=Notification.MESSAGE_TYPE_SUCCESS
    )
    return f'Mushaf {mushaf.name} imported successfully.'

@shared_task
def import_translation_task(translation_data, user_id):
    User = get_user_model()
    user = User.objects.get(id=user_id)
    with transaction.atomic():
        translator, _ = User.objects.get_or_create(username=translation_data["translator_username"])
        mushaf = Mushaf.objects.get(short_name=translation_data["mushaf"])
        translation = Translation.objects.create(
            creator_id=user.id,
            mushaf_id=mushaf.id,
            translator_id=translator.id,
            source=translation_data["source"],
            status="published",
            language=translation_data["language"],
        )
        # Build a lookup for Ayah objects of this mushaf keyed by (surah_number, ayah_number)
        ayah_lookup = {
            (a.surah.number, a.number): a.id
            for a in Ayah.objects.filter(surah__mushaf=mushaf).only("id", "number", "surah__number").select_related("surah")
        }

        ayah_translations = []
        # Root-level bismillah text (if provided) – used as default for all ayahs
        default_bismillah = translation_data.get("bismillah_text")

        for surah_data in translation_data["surahs"]:
            surah_number = surah_data["number"]
            for ayah_data in surah_data["ayah_translations"]:
                ayah_number = ayah_data["number"]
                ayah_id = ayah_lookup.get((surah_number, ayah_number))
                if ayah_id is None:
                    # Skip if corresponding ayah not found (data mismatch)
                    continue
                ayah_translations.append(
                    AyahTranslation(
                        creator_id=user.id,
                        translation_id=translation.id,
                        ayah_id=ayah_id,
                        text=ayah_data["text"],
                        bismillah=ayah_data.get("bismillah_text") or default_bismillah,
                    )
                )
        AyahTranslation.objects.bulk_create(ayah_translations)
    # Send notification to user
    Notification.objects.create(
        user=user,
        resource_controller="translations",
        resource_action="import",
        resource_uuid=translation.uuid,
        status=Notification.STATUS_NOTHING,
        description=f'Translation import complete',
        message=f'Translation {translation.uuid} imported successfully.',
        message_type=Notification.MESSAGE_TYPE_SUCCESS
    )
    return f'Translation {translation.uuid} imported successfully.'

@shared_task(serializer="pickle")
def generate_recitation_surah_timestamps_task(recitation, surah, file_obj):
    from quran.models import RecitationSurah, RecitationSurahTimestamp, Word
    
    # Get or create the RecitationSurah association
    recitation_surah, created = RecitationSurah.objects.get_or_create(
        recitation=recitation,
        surah=surah,
        defaults={"file": file_obj}
    )
    
    # If it already existed but had no file, attach the file
    if not created and not recitation_surah.file_id:
        recitation_surah.file = file_obj
        recitation_surah.save(update_fields=["file"])
    
    # Construct the audio URL using s3_uuid
    audio_url = file_obj.get_absolute_url()

    # Get all words in the surah, ordered by ayah number and id (creation order)
    words = list(Word.objects.filter(ayah__surah=surah).order_by('ayah__number', 'id'))
    text = ' '.join([w.text for w in words])
    user = getattr(recitation, 'creator', None)
    try:
        if audio_url and text:
            try:
                headers = {}
                if getattr(settings, 'FORCED_ALIGNMENT_SECRET_KEY', None):
                    if settings.FORCED_ALIGNMENT_SECRET_KEY:
                        headers['Authorization'] = settings.FORCED_ALIGNMENT_SECRET_KEY
                align_response = requests.post(
                    f'{settings.FORCED_ALIGNMENT_API_URL}/align',
                    json={
                        'mp3_url': audio_url,
                        'text': text,
                        'language': 'ar'
                    },
                    headers=headers if headers else None,
                    timeout=120
                )
                align_response.raise_for_status()
                alignment_data = align_response.json()
                from datetime import datetime, timedelta
                # Match force-alignment words to ayah words by text
                word_idx = 0
                for word_data in alignment_data:
                    # Ensure 'word' key exists in word_data
                    # Find the next matching word in ayah words
                    while word_idx < len(words) and words[word_idx].text != word_data['text']:
                        word_idx += 1
                    if word_idx < len(words):
                        word_obj = words[word_idx]
                        start_time = (datetime.min + timedelta(seconds=word_data['start'])).time()
                        end_time = (datetime.min + timedelta(seconds=word_data['end'])).time() if word_data.get('end') else None
                        # Ensure we have a RecitationSurah for this recitation/surah combo
                        # recitation_surah, _ = RecitationSurah.objects.get_or_create(
                        #     recitation=recitation,
                        #     surah=surah,
                        #     defaults={"file_id": getattr(recitation, "file_id", None)},
                        # )

                        RecitationSurahTimestamp.objects.create(
                            recitation_surah=recitation_surah,
                            start_time=start_time,
                            end_time=end_time,
                            word=word_obj
                        )
                        word_idx += 1
                    # If not matched, skip this word_data
                # Send notification to user if available
                if user:
                    Notification.objects.create(
                        user=user,
                        resource_controller="recitations",
                        resource_action="",
                        resource_uuid=getattr(recitation, 'uuid', None),
                        status=Notification.STATUS_NOTHING,
                        description=f'Recitation timestamps generated',
                        message=f'Recitation timestamps generated for recitation {getattr(recitation, "uuid", "")}.',
                        message_type=Notification.MESSAGE_TYPE_SUCCESS
                    )
                return 'timestamps generated'
            except Exception as e:
                # Send failure notification to user if available
                if user:
                    Notification.objects.create(
                        user=user,
                        resource_controller="quran.generate_recitation_timestamps",
                        resource_action="",
                        resource_uuid=getattr(recitation, 'uuid', None),
                        status=Notification.STATUS_NOTHING,
                        description=f'Failed to generate recitation timestamps',
                        message=f'Failed to generate recitation timestamps for recitation {getattr(recitation, "uuid", "")}: {str(e)}',
                        message_type=Notification.MESSAGE_TYPE_FAILED
                    )
                return f'Failed to generate timestamps: {str(e)}'
        else:
            # Send failure notification to user if available
            if user:
                Notification.objects.create(
                    user=user,
                    resource_controller="quran.tasks.generate_recitation_timestamps",
                    resource_action="",
                    resource_uuid=getattr(recitation, 'uuid', None),
                    status=Notification.STATUS_NOTHING,
                    description=f'Failed to generate recitation timestamps',
                    message=f'Failed to generate recitation timestamps for recitation {getattr(recitation, "uuid", "")}: missing audio_url or text',
                    message_type=Notification.MESSAGE_TYPE_FAILED
                )
            return 'Failed: missing audio_url or text'
    except Exception as e:
        # Send failure notification to user if available
        if user:
            Notification.objects.create(
                user=user,
                resource_controller="quran.tasks.generate_recitation_timestamps",
                resource_action="",
                resource_uuid=getattr(recitation, 'uuid', None),
                status=Notification.STATUS_NOTHING,
                description=f'Failed to generate recitation timestamps',
                message=f'Failed to generate recitation timestamps for recitation {getattr(recitation, "uuid", "")}: {str(e)}',
                message_type=Notification.MESSAGE_TYPE_FAILED
            )
        return f'Failed to generate timestamps: {str(e)}'
