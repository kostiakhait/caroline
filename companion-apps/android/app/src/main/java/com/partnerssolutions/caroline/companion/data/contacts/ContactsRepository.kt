package com.partnerssolutions.caroline.companion.data.contacts

import android.content.Context
import android.provider.ContactsContract

/**
 * Read-only contact list/search for companion_list_contacts/
 * companion_search_contacts (companion_plugin.py). Queries
 * ContactsContract.CommonDataKinds.Phone directly (name + number pairs,
 * one row per number) rather than Contacts + a separate Data join --
 * simpler, and Caroline only ever needs name+numbers, nothing else from
 * the contact record.
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
}
