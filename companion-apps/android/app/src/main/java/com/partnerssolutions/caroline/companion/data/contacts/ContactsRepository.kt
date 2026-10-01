package com.partnerssolutions.caroline.companion.data.contacts

import android.content.ContentProviderOperation
import android.content.ContentUris
import android.content.Context
import android.provider.ContactsContract

/**
 * List/search/create for companion_list_contacts/companion_search_contacts/
 * companion_create_contact (companion_plugin.py). list()/search() query
 * ContactsContract.CommonDataKinds.Phone directly (name + number pairs, one
 * row per number) rather than Contacts + a separate Data join -- simpler,
 * and Caroline only ever needs name+numbers, nothing else from the contact
 * record.
 */
class ContactsRepository(private val context: Context) {

    data class Contact(val name: String, val numbers: List<String>)

    /** All contacts with at least one phone number, grouped by contact id,
     * capped -- a full address book can be large and Caroline only ever
     * needs a bounded, recognizable list, not an exhaustive dump. */
    fun list(limit: Int = 500): List<Contact> = query(selection = null, args = null, limit = limit)

    /** Name OR number containing `query` (case-insensitive substring). */
    fun search(query: String, limit: Int = 100): List<Contact> {
        val like = "%$query%"
        return query(
            selection = "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ? OR " +
                "${ContactsContract.CommonDataKinds.Phone.NUMBER} LIKE ?",
            args = arrayOf(like, like),
            limit = limit,
        )
    }

    private fun query(selection: String?, args: Array<String>?, limit: Int): List<Contact> {
        val byId = LinkedHashMap<String, Pair<String, MutableList<String>>>()
        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
        )
        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI, projection, selection, args,
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} ASC",
        )?.use { c ->
            val idIdx = c.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.CONTACT_ID)
            val nameIdx = c.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
            val numberIdx = c.getColumnIndexOrThrow(ContactsContract.CommonDataKinds.Phone.NUMBER)
            while (c.moveToNext() && byId.size < limit) {
                val id = c.getString(idIdx) ?: continue
                val name = c.getString(nameIdx) ?: "(no name)"
                val number = c.getString(numberIdx) ?: continue
                val entry = byId.getOrPut(id) { name to mutableListOf() }
                if (number !in entry.second) entry.second.add(number)
            }
        }
        return byId.values.map { (name, numbers) -> Contact(name, numbers) }
    }

    /**
     * Saves a new contact (name + one or more phone numbers) via the
     * standard three-row RawContact/StructuredName/Phone batch insert
     * (ContactsContract's own documented pattern for a local, no-account
     * contact -- `account_name`/`account_type` left null, same as what
     * the stock Contacts app writes for "Phone-only" contacts). Returns
     * the new contact's id, or throws on failure (the caller already runs
     * this inside handleRequestFamily's own try/catch, which turns any
     * exception into a plain {"error": ...} response).
     */
    fun create(name: String, numbers: List<String>): String {
        val ops = ArrayList<ContentProviderOperation>()
        ops.add(
            ContentProviderOperation.newInsert(ContactsContract.RawContacts.CONTENT_URI)
                .withValue(ContactsContract.RawContacts.ACCOUNT_TYPE, null as String?)
                .withValue(ContactsContract.RawContacts.ACCOUNT_NAME, null as String?)
                .build(),
        )
        ops.add(
            ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, 0)
                .withValue(ContactsContract.Data.MIMETYPE, ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE)
                .withValue(ContactsContract.CommonDataKinds.StructuredName.DISPLAY_NAME, name)
                .build(),
        )
        for (number in numbers) {
            ops.add(
                ContentProviderOperation.newInsert(ContactsContract.Data.CONTENT_URI)
                    .withValueBackReference(ContactsContract.Data.RAW_CONTACT_ID, 0)
                    .withValue(ContactsContract.Data.MIMETYPE, ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE)
                    .withValue(ContactsContract.CommonDataKinds.Phone.NUMBER, number)
                    .withValue(ContactsContract.CommonDataKinds.Phone.TYPE, ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE)
                    .build(),
            )
        }
        val results = context.contentResolver.applyBatch(ContactsContract.AUTHORITY, ops)
        // results[0] is the RawContacts insert; its URI's last path segment
        // is the new raw_contact_id, which equals the contact's own id for
        // a single-raw-contact (no account merge) insert like this one.
        val rawContactUri = results[0].uri ?: throw IllegalStateException("Contact insert returned no uri")
        return ContentUris.parseId(rawContactUri).toString()
    }
}
